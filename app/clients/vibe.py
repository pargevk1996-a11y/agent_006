"""HTTP client for the Vibe-Marketolog Agent API.

Responsibilities kept strictly here: authentication, timeouts, retries, error
translation and log hygiene. No business logic, no pricing decisions.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import httpx

from app.clients.base import VibeClient
from app.clients.exceptions import (
    VibeAPIError,
    VibeNetworkError,
    VibePollTimeoutError,
)
from app.clients.retry import RetryPolicy, Sleeper, with_retry
from app.core.logging import get_correlation_id
from app.core.security import mask_secret

logger = logging.getLogger(__name__)

TERMINAL_STATUSES = {"complete", "completed", "success", "error", "failed", "cancelled"}
SUCCESS_STATUSES = {"complete", "completed", "success"}


class HttpVibeClient(VibeClient):
    """Async client over ``httpx.AsyncClient``."""

    mode = "http"

    def __init__(
        self,
        *,
        base_url: str,
        token: str | None,
        timeout: httpx.Timeout | None = None,
        retry_policy: RetryPolicy | None = None,
        client: httpx.AsyncClient | None = None,
        poll_interval: float = 10.0,
        poll_timeout: float = 900.0,
        sleeper: Sleeper | None = None,
    ) -> None:
        self._token = token
        self._base_url = base_url.rstrip("/")
        self._retry = retry_policy or RetryPolicy()
        self._poll_interval = poll_interval
        self._poll_timeout = poll_timeout
        self._sleep: Sleeper = sleeper or asyncio.sleep
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            base_url=self._base_url,
            timeout=timeout or httpx.Timeout(30.0, connect=5.0),
            headers={"Accept": "application/json", "User-Agent": "vibe-budget-agent/0.1"},
        )

    # -- infrastructure ----------------------------------------------------
    def _auth_headers(self) -> dict[str, str]:
        headers: dict[str, str] = {}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        cid = get_correlation_id()
        if cid:
            headers["X-Correlation-Id"] = cid
        return headers

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def _request(
        self, method: str, path: str, *, json_body: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        async def attempt() -> dict[str, Any]:
            started = time.monotonic()
            try:
                response = await self._client.request(
                    method, path, json=json_body, headers=self._auth_headers()
                )
            except httpx.TimeoutException as exc:
                raise VibeNetworkError(f"timeout calling {method} {path}: {exc!r}") from exc
            except httpx.HTTPError as exc:
                raise VibeNetworkError(f"transport error on {method} {path}: {exc!r}") from exc

            elapsed_ms = round((time.monotonic() - started) * 1000, 1)
            payload = _safe_json(response)
            logger.info(
                "vibe_api_call",
                extra={
                    "http_method": method,
                    "path": path,
                    "status_code": response.status_code,
                    "elapsed_ms": elapsed_ms,
                    "upstream_request_id": payload.get("request_id"),
                    "token": mask_secret(self._token),
                },
            )
            if response.status_code >= 400:
                raise _to_api_error(response, payload)
            return payload

        return await with_retry(
            attempt,
            policy=self._retry,
            sleeper=self._sleep,
            on_retry=lambda attempt_no, delay, error: logger.warning(
                "vibe_api_retry",
                extra={
                    "path": path,
                    "attempt": attempt_no,
                    "sleep_seconds": round(delay, 3),
                    "error": str(error),
                },
            ),
        )

    # -- endpoints ---------------------------------------------------------
    async def capabilities(self) -> dict[str, Any]:
        return await self._request("GET", "/capabilities")

    async def me(self) -> dict[str, Any]:
        return await self._request("GET", "/me")

    async def balance(self) -> dict[str, Any]:
        return await self._request("GET", "/balance")

    async def estimate(self, body: dict[str, Any]) -> dict[str, Any]:
        return await self._request("POST", "/generate/estimate", json_body=body)

    async def generate(self, body: dict[str, Any]) -> dict[str, Any]:
        if not body.get("idempotency_key"):
            raise ValueError("refusing to call /generate without an idempotency_key")
        if body.get("strict") is not True:
            raise ValueError("refusing to call /generate without strict=true")
        return await self._request("POST", "/generate", json_body=body)

    async def generation_status(self, generation_id: int | str) -> dict[str, Any]:
        return await self._request("GET", f"/generation/{generation_id}/status")

    async def voiceover_status(self, voiceover_id: int | str) -> dict[str, Any]:
        """Long voiceover progress: /generate returns voiceover_id instead of a
        generation_id when the prompt exceeds the model's single-request limit."""
        return await self._request("GET", f"/voiceover/long/{voiceover_id}")

    async def wait_for_generation(
        self,
        generation_id: int | str,
        *,
        interval: float | None = None,
        timeout: float | None = None,  # noqa: ASYNC109 - polling budget, not a cancel scope
    ) -> dict[str, Any]:
        """Poll until terminal state, a hard timeout, or a non-retryable error."""
        interval = interval or self._poll_interval
        deadline = (timeout or self._poll_timeout)
        waited = 0.0
        while True:
            payload = await self.generation_status(generation_id)
            status = str(payload.get("status", "")).lower()
            if status in TERMINAL_STATUSES:
                return payload
            if waited >= deadline:
                raise VibePollTimeoutError(generation_id, waited)
            await self._sleep(interval)
            waited += interval


def _safe_json(response: httpx.Response) -> dict[str, Any]:
    try:
        payload = response.json()
    except ValueError:
        return {"raw_body": response.text[:500]}
    return payload if isinstance(payload, dict) else {"data": payload}


def _to_api_error(response: httpx.Response, payload: dict[str, Any]) -> VibeAPIError:
    retry_after = payload.get("retry_after")
    if retry_after is None:
        header = response.headers.get("Retry-After")
        if header:
            try:
                retry_after = float(header)
            except ValueError:
                retry_after = None
    return VibeAPIError(
        response.status_code,
        code=str(payload.get("error") or f"http_{response.status_code}"),
        message=str(payload.get("message") or response.reason_phrase or ""),
        details=payload.get("details"),
        request_id=payload.get("request_id"),
        retry_after=float(retry_after) if isinstance(retry_after, (int, float)) else None,
    )
