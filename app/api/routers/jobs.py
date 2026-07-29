"""Execution reporting."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Path

from app.api.deps import get_plan_service
from app.api.schemas import JobResponse
from app.services.plan_service import PlanService

router = APIRouter(prefix="/api/v1/jobs", tags=["jobs"])


@router.get(
    "/{job_id}",
    response_model=JobResponse,
    summary="Статус исполнения, ссылки на результаты и фактическая стоимость",
)
async def get_job(
    job_id: str = Path(...),
    service: PlanService = Depends(get_plan_service),
) -> JobResponse:
    return JobResponse.from_job(await service.get_job(job_id))
