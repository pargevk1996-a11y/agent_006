"""Shared fixtures. Nothing here touches the network or spends money."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import httpx
import pytest

from app.clients.mock import MockVibeClient
from app.core.config import AppMode, Settings
from app.domain.brief import Brief
from app.domain.capabilities import Capabilities
from app.domain.policy import Policy
from app.repositories.db import Database
from app.repositories.jobs import JobRepository
from app.repositories.plans import PlanRepository
from app.services.plan_service import PlanService

BRIEF_KWARGS = {
    "product_name": "CRM для мастеров маникюра",
    "product_description": "Онлайн-запись, напоминания клиентам и учёт расходников.",
    "target_audience": "Мастера маникюра, 22–40 лет",
    "offer": "Первый месяц бесплатно",
    "style": "дружелюбный, без канцелярита",
}


def make_brief(**overrides) -> Brief:
    payload = {**BRIEF_KWARGS, "formats": ["image"], "budget_rub": 500.0, **overrides}
    return Brief(**payload)


def make_settings(tmp_path: Path, **overrides) -> Settings:
    defaults = {
        "app_mode": AppMode.MOCK,
        "db_path": tmp_path / "test.db",
        "budget_safety_margin": 0.0,
        "poll_interval_seconds": 0.001,
        "poll_timeout_seconds": 1.0,
        "_env_file": None,  # never read a developer's real .env during tests
    }
    return Settings(**{**defaults, **overrides})


@pytest.fixture
def policy() -> Policy:
    return Policy.load(None)


@pytest.fixture
def capabilities() -> Capabilities:
    return Capabilities.parse(MockVibeClient().
                              _payload)  # type: ignore[attr-defined]


@pytest.fixture
async def database(tmp_path: Path) -> Database:
    db = Database(tmp_path / "test.db")
    await db.connect()
    yield db
    await db.close()


@pytest.fixture
def client() -> MockVibeClient:
    return MockVibeClient(balance_rub=5000.0, daily_limit_rub=5000.0)


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return make_settings(tmp_path)


@pytest.fixture
def service(client: MockVibeClient, settings: Settings, database: Database, policy: Policy):
    return PlanService(
        client=client,
        settings=settings,
        policy=policy,
        plan_repo=PlanRepository(database),
        job_repo=JobRepository(database),
    )


@pytest.fixture
def webhook_secret() -> str:
    return "whsec_test_value"


@pytest.fixture
def otel_spans():
    """Real OpenTelemetry SDK exporting into memory.

    The tracing adapter is optional in production, so it is verified here against
    the actual SDK rather than only in its no-op form: a span that silently loses
    the cost attribute is worse than no span at all.
    """
    from opentelemetry import trace
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    # The global provider can only be set once per process; swap the private slot
    # so each test gets a clean recorder instead of leaking spans into the next.
    previous = trace._TRACER_PROVIDER
    trace._TRACER_PROVIDER = provider
    try:
        yield exporter.get_finished_spans
    finally:
        trace._TRACER_PROVIDER = previous


@pytest.fixture
async def api_client(tmp_path: Path, webhook_secret: str):
    """The real ASGI app in mock mode: full HTTP surface, no network, no spending."""
    from app.main import create_app

    settings = make_settings(
        tmp_path, db_path=tmp_path / "api.db", vibe_webhook_secret=webhook_secret
    )
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            yield client


def make_service(client, settings, database, policy=None, queue=None) -> PlanService:
    return PlanService(
        client=client,
        settings=settings,
        policy=policy or Policy.load(None),
        plan_repo=PlanRepository(database),
        job_repo=JobRepository(database),
        queue=queue,
    )


TERMINAL_JOB_STATUSES = {"succeeded", "partial", "failed", "aborted"}


async def wait_for_job(
    api_client, job_id: str, *, timeout: float = 5.0  # noqa: ASYNC109 - polling budget
) -> dict:
    """Poll a job until it settles — the client-side half of async execution."""
    deadline = time.monotonic() + timeout
    while True:
        body = (await api_client.get(f"/api/v1/jobs/{job_id}")).json()
        if body["status"] in TERMINAL_JOB_STATUSES:
            return body
        if time.monotonic() > deadline:
            raise AssertionError(f"job {job_id} завис в статусе {body['status']}")
        await asyncio.sleep(0.01)


async def execute_and_wait(
    api_client, plan_id: str, *, timeout: float = 5.0  # noqa: ASYNC109 - polling budget
) -> dict:
    """POST /execute (async) and poll until the job reaches a terminal status."""
    response = await api_client.post(
        f"/api/v1/plans/{plan_id}/execute", json={"confirmed": True}
    )
    assert response.status_code in {200, 202}, response.text
    return await wait_for_job(api_client, response.json()["job_id"], timeout=timeout)
