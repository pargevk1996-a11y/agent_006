"""Planning and execution endpoints."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Path

from app.api.deps import get_plan_service
from app.api.schemas import (
    CreatePlanRequest,
    ExecuteRequest,
    JobResponse,
    PlanResponse,
)
from app.services.plan_service import PlanService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/plans", tags=["plans"])


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
    service: PlanService = Depends(get_plan_service),
) -> PlanResponse:
    plan = await service.create_plan(payload)
    return PlanResponse.from_plan(plan)


@router.get("/{plan_id}", response_model=PlanResponse, summary="Получить сохранённый план")
async def get_plan(
    plan_id: str = Path(...),
    service: PlanService = Depends(get_plan_service),
) -> PlanResponse:
    return PlanResponse.from_plan(await service.get_plan(plan_id))


@router.post(
    "/{plan_id}/execute",
    response_model=JobResponse,
    summary="Запустить план после явного подтверждения",
    description=(
        "Требует {\"confirmed\": true}. Перед запуском повторно проверяет цену каждого шага, "
        "баланс и дневной лимит: при росте цены или нехватке бюджета возвращает job со "
        "статусом aborted и нулевым списанием."
    ),
)
async def execute_plan(
    payload: ExecuteRequest,
    plan_id: str = Path(...),
    service: PlanService = Depends(get_plan_service),
) -> JobResponse:
    job = await service.execute_plan(plan_id, confirmed=payload.confirmed)
    return JobResponse.from_job(job)
