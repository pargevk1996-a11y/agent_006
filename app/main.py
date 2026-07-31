"""FastAPI application: wiring, middleware, error handling."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.api.deps import build_client, build_plan_service
from app.api.routers import health, jobs, media, metrics, plans, webhooks
from app.api.schemas import ErrorResponse
from app.clients.exceptions import VibeAPIError, VibeError
from app.core.config import AppMode, Settings, get_settings
from app.core.errors import AppError
from app.core.logging import configure_logging, get_correlation_id, set_correlation_id
from app.core.security import mask_secret
from app.domain.policy import Policy
from app.repositories.db import Database
from app.repositories.idempotency import IdempotencyRepository
from app.repositories.jobs import JobRepository
from app.repositories.media import MediaRepository
from app.services.job_queue import JobQueue
from app.services.reconciler import Reconciler
from app.services.retention import RetentionService

logger = logging.getLogger(__name__)

CORRELATION_HEADER = "X-Correlation-Id"


async def _recover_unfinished_jobs(database: Database, queue: JobQueue) -> None:
    """Re-queue jobs a previous process left mid-flight.

    Safe by construction: the step ledger already holds the ``generation_id`` of
    everything that was launched, so a resumed job waits for those results
    instead of paying for them again.
    """
    repo = JobRepository(database)
    job_ids = await repo.unfinished_job_ids()
    for job_id in job_ids:
        await repo.requeue(job_id)
        await queue.enqueue(job_id)
    if job_ids:
        logger.warning("jobs_recovered", extra={"count": len(job_ids), "job_ids": job_ids})


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings.log_level, settings.log_format, secrets=settings.secret_values())

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        database = Database(settings.db_path)
        await database.connect()
        app.state.settings = settings
        app.state.database = database
        app.state.policy = Policy.load(settings.policy_file)
        app.state.client = build_client(settings)

        async def run_job(job_id: str) -> None:
            await build_plan_service(app.state).run_job(job_id)

        queue = JobQueue(run_job, concurrency=settings.executor_concurrency)
        app.state.job_queue = queue
        await queue.start()
        await _recover_unfinished_jobs(database, queue)

        reconciler = Reconciler(
            client=app.state.client,
            job_repo=JobRepository(database),
            interval_seconds=settings.reconcile_interval_seconds,
            min_age_seconds=settings.reconcile_min_age_seconds,
        )
        app.state.reconciler = reconciler
        await reconciler.start()

        retention = RetentionService(
            idempotency=IdempotencyRepository(database),
            media=MediaRepository(database),
            interval_seconds=settings.retention_interval_seconds,
            idempotency_ttl_hours=settings.idempotency_ttl_hours,
        )
        app.state.retention = retention
        await retention.start()

        logger.info(
            "app_started",
            extra={
                "mode": settings.app_mode.value,
                "env": settings.app_env,
                "db_path": str(settings.db_path),
                "policy_source": app.state.policy.source,
                "token": mask_secret(settings.token_value),
                "webhook_secret_configured": bool(settings.webhook_secret_value),
                "callback_url": settings.callback_url or "<polling only>",
                "executor_workers": settings.executor_concurrency,
                "reconcile_interval": settings.reconcile_interval_seconds,
            },
        )
        if settings.app_mode is AppMode.LIVE:
            logger.warning(
                "live_mode_enabled",
                extra={"note": "платные генерации разрешены после confirmed=true"},
            )
        try:
            yield
        finally:
            await retention.stop()
            await reconciler.stop()
            await queue.stop()
            await app.state.client.aclose()
            await database.close()
            logger.info("app_stopped")

    app = FastAPI(
        title="Vibe Budget Agent",
        version=health.VERSION,
        description=(
            "Cost-aware агент: маркетинговый бриф → план генерации контента, который "
            "гарантированно не выходит за рублёвый бюджет. Планирование бесплатно, "
            "исполнение — только после явного подтверждения."
        ),
        lifespan=lifespan,
    )

    @app.middleware("http")
    async def correlation_middleware(request: Request, call_next):
        cid = set_correlation_id(request.headers.get(CORRELATION_HEADER))
        logger.info(
            "request_started",
            extra={"http_method": request.method, "path": request.url.path},
        )
        response = await call_next(request)
        response.headers[CORRELATION_HEADER] = cid
        logger.info(
            "request_finished",
            extra={
                "http_method": request.method,
                "path": request.url.path,
                "status_code": response.status_code,
            },
        )
        return response

    @app.exception_handler(AppError)
    async def app_error_handler(_: Request, exc: AppError) -> JSONResponse:
        logger.warning("app_error", extra={"error_code": exc.code, "detail": exc.message})
        return JSONResponse(
            status_code=exc.status_code,
            content=ErrorResponse(
                error=exc.code,
                message=exc.message,
                details=exc.details,
                correlation_id=get_correlation_id(),
            ).model_dump(),
        )

    @app.exception_handler(VibeError)
    async def vibe_error_handler(_: Request, exc: VibeError) -> JSONResponse:
        code = exc.code if isinstance(exc, VibeAPIError) else "upstream_unavailable"
        status = 502
        if isinstance(exc, VibeAPIError) and exc.status_code in {401, 402, 403, 429}:
            status = exc.status_code
        logger.error("upstream_error", extra={"error_code": code, "detail": str(exc)})
        return JSONResponse(
            status_code=status,
            content=ErrorResponse(
                error=code,
                message=str(exc),
                correlation_id=get_correlation_id(),
            ).model_dump(),
        )

    @app.exception_handler(RequestValidationError)
    async def validation_handler(_: Request, exc: RequestValidationError) -> JSONResponse:
        return JSONResponse(
            status_code=422,
            content=ErrorResponse(
                error="validation_failed",
                message="Запрос не прошёл валидацию.",
                details=exc.errors(),
                correlation_id=get_correlation_id(),
            ).model_dump(mode="json"),
        )

    app.include_router(health.router)
    app.include_router(plans.router)
    app.include_router(jobs.router)
    app.include_router(media.router)
    app.include_router(metrics.router)
    app.include_router(webhooks.router)
    return app


app = create_app()
