"""Dependency wiring: one client, one database, one policy per process."""

from __future__ import annotations

import logging
from typing import Any

import httpx
from fastapi import Request

from app.clients.base import VibeClient
from app.clients.mock import MockVibeClient
from app.clients.retry import RetryPolicy
from app.clients.vibe import HttpVibeClient
from app.core.config import AppMode, Settings
from app.domain.policy import Policy
from app.repositories.db import Database
from app.repositories.jobs import JobRepository
from app.repositories.plans import PlanRepository
from app.services.job_queue import JobQueue
from app.services.plan_service import PlanService

logger = logging.getLogger(__name__)


def build_client(settings: Settings) -> VibeClient:
    """Mock mode never constructs an HTTP client — it cannot reach the network at all."""
    if settings.app_mode is AppMode.MOCK:
        logger.info(
            "client_mode_mock",
            extra={"balance_rub": settings.mock_balance_rub, "network": "disabled"},
        )
        return MockVibeClient(
            balance_rub=settings.mock_balance_rub,
            daily_limit_rub=settings.mock_daily_limit_rub,
            daily_spent_rub=settings.mock_daily_spent_rub,
        )
    return HttpVibeClient(
        base_url=settings.vibe_base_url,
        token=settings.token_value,
        timeout=httpx.Timeout(
            settings.http_total_timeout,
            connect=settings.http_connect_timeout,
            read=settings.http_read_timeout,
        ),
        retry_policy=RetryPolicy(
            max_attempts=settings.retry_max_attempts,
            base_delay=settings.retry_base_delay,
            max_delay=settings.retry_max_delay,
            jitter=settings.retry_jitter,
        ),
        poll_interval=settings.poll_interval_seconds,
        poll_timeout=settings.poll_timeout_seconds,
    )


def get_settings(request: Request) -> Settings:
    return request.app.state.settings


def get_database(request: Request) -> Database:
    return request.app.state.database


def get_client(request: Request) -> VibeClient:
    return request.app.state.client


def get_policy(request: Request) -> Policy:
    return request.app.state.policy


def build_plan_service(state: Any) -> PlanService:
    """Assemble the service from application state.

    Shared by the request path and the background workers — a worker has no
    ``Request``, but needs exactly the same wiring.
    """
    return PlanService(
        client=state.client,
        settings=state.settings,
        policy=state.policy,
        plan_repo=PlanRepository(state.database),
        job_repo=JobRepository(state.database),
        queue=getattr(state, "job_queue", None),
    )


def get_plan_service(request: Request) -> PlanService:
    return build_plan_service(request.app.state)


def get_job_queue(request: Request) -> JobQueue | None:
    return getattr(request.app.state, "job_queue", None)


def get_job_repository(request: Request) -> JobRepository:
    return JobRepository(request.app.state.database)
