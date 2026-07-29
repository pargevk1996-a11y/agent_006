"""Shared fixtures. Nothing here touches the network or spends money."""

from __future__ import annotations

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


def make_service(client, settings, database, policy=None) -> PlanService:
    return PlanService(
        client=client,
        settings=settings,
        policy=policy or Policy.load(None),
        plan_repo=PlanRepository(database),
        job_repo=JobRepository(database),
    )
