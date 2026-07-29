"""Retries: transient failures only, with backoff and jitter — never on plain 4xx."""

from __future__ import annotations

import random

import httpx
import pytest

from app.clients.exceptions import VibeAPIError, VibeNetworkError
from app.clients.retry import RetryPolicy, with_retry
from app.clients.vibe import HttpVibeClient


class Recorder:
    """Collects sleep durations instead of actually waiting."""

    def __init__(self) -> None:
        self.sleeps: list[float] = []

    async def __call__(self, delay: float) -> None:
        self.sleeps.append(delay)


def make_client(handler, **kwargs) -> HttpVibeClient:
    transport = httpx.MockTransport(handler)
    http = httpx.AsyncClient(transport=transport, base_url="https://api.test")
    return HttpVibeClient(base_url="https://api.test", token="secret-token", client=http, **kwargs)


class TestRetryPolicy:
    def test_backoff_grows_exponentially(self):
        policy = RetryPolicy(base_delay=1.0, max_delay=100.0, jitter=0.0)
        assert [policy.delay_for(i) for i in (1, 2, 3, 4)] == [1.0, 2.0, 4.0, 8.0]

    def test_backoff_is_capped(self):
        policy = RetryPolicy(base_delay=1.0, max_delay=3.0, jitter=0.0)
        assert policy.delay_for(10) == 3.0

    def test_jitter_stays_within_band_and_is_nonzero_spread(self):
        policy = RetryPolicy(base_delay=1.0, max_delay=100.0, jitter=0.5)
        rng = random.Random(1234)
        delays = [policy.delay_for(2, rng=rng) for _ in range(50)]
        assert all(1.0 <= d <= 3.0 for d in delays)
        assert len(set(delays)) > 1, "jitter должен разносить попытки во времени"

    def test_retry_after_header_wins(self):
        policy = RetryPolicy(base_delay=1.0, max_delay=60.0, jitter=0.5)
        assert policy.delay_for(1, retry_after=12.0) == 12.0

    def test_retry_after_is_capped_by_max_delay(self):
        policy = RetryPolicy(base_delay=1.0, max_delay=5.0)
        assert policy.delay_for(1, retry_after=3600) == 5.0

    @pytest.mark.parametrize("status", [429, 500, 502, 503])
    def test_transient_statuses_are_retryable(self, status):
        assert VibeAPIError(status).retryable

    @pytest.mark.parametrize("status", [400, 401, 403, 404, 422])
    def test_client_errors_are_not_retryable(self, status):
        assert not VibeAPIError(status).retryable

    async def test_stops_after_max_attempts(self):
        policy = RetryPolicy(max_attempts=3, base_delay=0.0, jitter=0.0)
        recorder = Recorder()
        attempts = 0

        async def always_fails():
            nonlocal attempts
            attempts += 1
            raise VibeNetworkError("boom")

        with pytest.raises(VibeNetworkError):
            await with_retry(always_fails, policy=policy, sleeper=recorder)
        assert attempts == 3
        assert len(recorder.sleeps) == 2


class TestClientRetryBehaviour:
    async def test_retries_network_errors_then_succeeds(self):
        calls = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            if calls["n"] < 3:
                raise httpx.ConnectError("connection reset", request=request)
            return httpx.Response(200, json={"status": "ok"})

        recorder = Recorder()
        client = make_client(
            handler,
            retry_policy=RetryPolicy(max_attempts=4, base_delay=0.01, jitter=0.0),
            sleeper=recorder,
        )
        assert await client.capabilities() == {"status": "ok"}
        assert calls["n"] == 3
        assert len(recorder.sleeps) == 2
        await client.aclose()

    async def test_retries_429_and_honours_retry_after(self):
        calls = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            if calls["n"] == 1:
                return httpx.Response(
                    429,
                    headers={"Retry-After": "7"},
                    json={"status": "error", "error": "rate_limit_exceeded", "message": "slow"},
                )
            return httpx.Response(200, json={"status": "ok"})

        recorder = Recorder()
        client = make_client(
            handler,
            retry_policy=RetryPolicy(max_attempts=3, base_delay=0.01, max_delay=30, jitter=0.0),
            sleeper=recorder,
        )
        await client.capabilities()
        assert calls["n"] == 2
        assert recorder.sleeps == [7.0]
        await client.aclose()

    async def test_retries_5xx(self):
        calls = {"n": 0}

        def handler(_request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            if calls["n"] < 2:
                return httpx.Response(503, json={"error": "internal_error"})
            return httpx.Response(200, json={"status": "ok"})

        client = make_client(
            handler,
            retry_policy=RetryPolicy(max_attempts=3, base_delay=0.0, jitter=0.0),
            sleeper=Recorder(),
        )
        await client.capabilities()
        assert calls["n"] == 2
        await client.aclose()

    @pytest.mark.parametrize(
        ("status", "code"),
        [(400, "bad_request"), (401, "invalid_token"), (402, "insufficient_balance"),
         (403, "insufficient_scope"), (404, "not_found"), (422, "validation_failed")],
    )
    async def test_no_retry_on_4xx(self, status, code):
        calls = {"n": 0}

        def handler(_request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            return httpx.Response(
                status,
                json={"status": "error", "error": code, "message": "нет", "request_id": "rid-1"},
            )

        recorder = Recorder()
        client = make_client(
            handler,
            retry_policy=RetryPolicy(max_attempts=5, base_delay=0.0, jitter=0.0),
            sleeper=recorder,
        )
        with pytest.raises(VibeAPIError) as exc_info:
            await client.capabilities()

        assert calls["n"] == 1, "обычные 4xx не повторяются"
        assert recorder.sleeps == []
        assert exc_info.value.code == code
        assert exc_info.value.request_id == "rid-1"
        await client.aclose()

    async def test_generate_is_not_retried_on_validation_error(self):
        """A rejected /generate must not be replayed — one charge risk is enough."""
        calls = {"n": 0}

        def handler(_request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            return httpx.Response(422, json={"error": "validation_failed", "message": "bad"})

        client = make_client(
            handler,
            retry_policy=RetryPolicy(max_attempts=5, base_delay=0.0, jitter=0.0),
            sleeper=Recorder(),
        )
        with pytest.raises(VibeAPIError):
            await client.generate(
                {"type": "image", "model": "z-image", "prompt": "x",
                 "strict": True, "idempotency_key": "k"}
            )
        assert calls["n"] == 1
        await client.aclose()

    async def test_timeout_is_translated_and_retried(self):
        calls = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            raise httpx.ReadTimeout("too slow", request=request)

        client = make_client(
            handler,
            retry_policy=RetryPolicy(max_attempts=2, base_delay=0.0, jitter=0.0),
            sleeper=Recorder(),
        )
        with pytest.raises(VibeNetworkError):
            await client.balance()
        assert calls["n"] == 2
        await client.aclose()
