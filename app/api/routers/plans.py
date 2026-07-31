"""Planning and execution endpoints."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Header, Path, Response

from app.api.deps import get_idempotency_store, get_plan_service
from app.api.idempotency import with_idempotency
from app.api.schemas import (
    CreatePlanRequest,
    ExecuteRequest,
    JobResponse,
    PlanResponse,
)
from app.domain.plan import JobStatus
from app.repositories.idempotency import IdempotencyRepository
from app.services.plan_service import PlanService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/plans", tags=["plans"])

IDEMPOTENCY_KEY = Header(
    default=None,
    alias="Idempotency-Key",
    description=(
        "Необязательный ключ повтора. Повторный запрос с тем же ключом и тем же телом "
        "возвращает сохранённый ответ первой попытки (заголовок Idempotency-Replayed: true) "
        "и не выполняет работу заново; тот же ключ с другим телом — 409."
    ),
)


@router.post(
    "",
    response_model=PlanResponse,
    status_code=201,
    summary="Построить план генерации в рамках бюджета (без списаний)",
    description=(
        "Читает GET /capabilities, снимает состояние аккаунта, подбирает совместимые модели "
        "и оценивает каждый шаг через POST /generate/estimate. Деньги не списываются."
    ),
)
async def create_plan(
    payload: CreatePlanRequest,
    response: Response,
    service: PlanService = Depends(get_plan_service),
    store: IdempotencyRepository = Depends(get_idempotency_store),
    idempotency_key: str | None = IDEMPOTENCY_KEY,
):
    async def produce() -> tuple[PlanResponse, int]:
        plan = await service.create_plan(payload)
        return PlanResponse.from_plan(plan), 201

    return await with_idempotency(
        store=store,
        key=idempotency_key,
        endpoint="POST /api/v1/plans",
        body=payload.model_dump(mode="json"),
        response=response,
        produce=produce,
    )


@router.get("/{plan_id}", response_model=PlanResponse, summary="Получить сохранённый план")
async def get_plan(
    plan_id: str = Path(...),
    service: PlanService = Depends(get_plan_service),
) -> PlanResponse:
    return PlanResponse.from_plan(await service.get_plan(plan_id))


@router.post(
    "/{plan_id}/execute",
    response_model=JobResponse,
    status_code=202,
    summary="Принять план к исполнению после явного подтверждения",
    description=(
        "Требует {\"confirmed\": true}. Возвращает 202 и job со статусом queued — "
        "исполнение идёт в фоне, следить за ним через GET /api/v1/jobs/{job_id} "
        "(адрес продублирован в заголовке Location) или по вебхуку. Уже перед первым "
        "рублём воркер заново проверяет цену каждого шага, баланс и дневной лимит: при "
        "росте цены или нехватке бюджета job переходит в aborted с нулевым списанием. "
        "Передайте {\"wait\": true}, чтобы дождаться результата прямо в этом запросе (200)."
    ),
    responses={200: {"description": "Задание уже завершено (wait=true или повторный вызов)"}},
)
async def execute_plan(
    payload: ExecuteRequest,
    response: Response,
    plan_id: str = Path(...),
    service: PlanService = Depends(get_plan_service),
    store: IdempotencyRepository = Depends(get_idempotency_store),
    idempotency_key: str | None = IDEMPOTENCY_KEY,
):
    async def produce() -> tuple[JobResponse, int]:
        job = await service.execute_plan(
            plan_id, confirmed=payload.confirmed, wait=payload.wait
        )
        response.headers["Location"] = f"/api/v1/jobs/{job.job_id}"
        return JobResponse.from_job(job), 202 if job.status is JobStatus.QUEUED else 200

    return await with_idempotency(
        store=store,
        key=idempotency_key,
        # Scoped to the plan: the same key against a different plan is a different
        # request, not a retry.
        endpoint=f"POST /api/v1/plans/{plan_id}/execute",
        body=payload.model_dump(mode="json"),
        response=response,
        produce=produce,
    )
