"""Request/response models of this service's own API."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from app.domain.brief import Brief, ContentFormat
from app.domain.plan import (
    AccountSnapshot,
    Job,
    JobStatus,
    Plan,
    PlanStatus,
    RejectedCandidate,
    StepKind,
    StepStatus,
)


class CreatePlanRequest(Brief):
    """A marketing brief plus its hard ruble ceiling."""

    model_config = {
        "json_schema_extra": {
            "example": {
                "product_name": "CRM для мастеров маникюра",
                "product_description": "Онлайн-запись, напоминания клиентам и учёт расходников.",
                "target_audience": "Мастера маникюра и небольшие студии, 22–40 лет",
                "offer": "Первый месяц бесплатно, перенос базы клиентов за нас",
                "formats": ["text", "image", "voice"],
                "budget_rub": 120,
                "style": "дружелюбный, живой, без канцелярита",
                "aspect_ratio": "9:16",
                "text_max_tokens": 900,
            }
        }
    }


class ExecuteRequest(BaseModel):
    """Explicit confirmation gate. Without ``confirmed: true`` nothing is spent."""

    confirmed: bool = Field(
        default=False,
        description="Must be true. Any other value refuses execution without spending.",
    )
    wait: bool = Field(
        default=False,
        description=(
            "Дождаться завершения в этом же запросе и вернуть 200 вместо 202. "
            "Удобно для демо и коротких планов; по умолчанию исполнение асинхронное."
        ),
    )

    model_config = {"json_schema_extra": {"example": {"confirmed": True}}}


class PlanStepResponse(BaseModel):
    step_id: str
    format: ContentFormat
    kind: StepKind
    type: str | None
    model: str | None
    model_description: str
    params: dict[str, Any]
    idempotency_key: str
    estimated_cost_rub: float
    cost_source: str
    cost_basis: str
    reason: str
    rejected_alternatives: list[RejectedCandidate]
    warnings: list[str]
    local_output: str | None = None


class PlanResponse(BaseModel):
    plan_id: str
    created_at: datetime
    mode: str
    status: PlanStatus
    currency: str = "RUB"
    budget_rub: float
    safety_margin_rub: float
    spendable_rub: float
    total_estimated_rub: float
    budget_remaining_rub: float
    steps: list[PlanStepResponse]
    warnings: list[str]
    account: AccountSnapshot
    capabilities_fingerprint: str
    job_id: str | None = None

    @classmethod
    def from_plan(cls, plan: Plan) -> PlanResponse:
        return cls(
            plan_id=plan.plan_id,
            created_at=plan.created_at,
            mode=plan.mode,
            status=plan.status,
            budget_rub=plan.budget_rub,
            safety_margin_rub=plan.safety_margin_rub,
            spendable_rub=plan.spendable_rub,
            total_estimated_rub=plan.total_estimated_rub,
            budget_remaining_rub=plan.budget_remaining_rub,
            steps=[PlanStepResponse(**step.model_dump()) for step in plan.steps],
            warnings=plan.warnings,
            account=plan.account,
            capabilities_fingerprint=plan.capabilities_fingerprint,
            job_id=plan.job_id,
        )


class JobStepResponse(BaseModel):
    step_id: str
    format: ContentFormat
    kind: StepKind
    type: str | None
    model: str | None
    status: StepStatus
    generation_id: int | None
    idempotency_key: str
    estimated_cost_rub: float
    actual_cost_rub: float
    refunded: bool
    display_url: str | None
    result_url: str | None
    local_output: str | None
    text_output: str | None
    error: str | None
    duration_seconds: float | None


class JobResponse(BaseModel):
    job_id: str
    plan_id: str
    mode: str
    status: JobStatus
    currency: str = "RUB"
    budget_rub: float
    estimated_cost_rub: float
    actual_cost_rub: float
    budget_remaining_rub: float
    steps: list[JobStepResponse]
    warnings: list[str]
    errors: list[str]
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    duration_seconds: float | None

    @classmethod
    def from_job(cls, job: Job) -> JobResponse:
        return cls(
            job_id=job.job_id,
            plan_id=job.plan_id,
            mode=job.mode,
            status=job.status,
            budget_rub=job.budget_rub,
            estimated_cost_rub=job.estimated_cost_rub,
            actual_cost_rub=job.actual_cost_rub,
            budget_remaining_rub=job.budget_remaining_rub,
            steps=[
                JobStepResponse(
                    **step.model_dump(exclude={"task_id", "attempts", "started_at", "finished_at"}),
                    duration_seconds=step.duration_seconds,
                )
                for step in job.steps
            ],
            warnings=job.warnings,
            errors=job.errors,
            created_at=job.created_at,
            started_at=job.started_at,
            finished_at=job.finished_at,
            duration_seconds=job.duration_seconds,
        )


class MediaUploadResponse(BaseModel):
    """A stable URL for a locally uploaded file, ready to paste into a brief."""

    url: str
    filename: str
    content_type: str
    size_bytes: int
    kind: str
    expires_in_days: int
    usage: str = (
        "Подставьте url в reference_image_urls брифа — это разблокирует image-to-video "
        "модели (veo3.1, kling-3.0, grok-itv), которым нужен image_urls."
    )


class MediaKindResponse(BaseModel):
    kind: str
    max_bytes: int
    max_megabytes: int
    extensions: list[str]


class MediaLimitsResponse(BaseModel):
    kinds: list[MediaKindResponse]
    ttl_days: int
    source: str  # capabilities | fallback


class HealthResponse(BaseModel):
    status: str = "ok"
    mode: str
    version: str
    database: str
    upstream: str
    live_spending_enabled: bool
    queue_depth: int = 0          # jobs admitted but not yet picked up
    executor_workers: int = 0     # how many can run at once


class WebhookAck(BaseModel):
    status: str = "accepted"
    generation_id: int | None = None
    matched_step: str | None = None


class ErrorResponse(BaseModel):
    status: str = "error"
    error: str
    message: str
    details: Any = None
    correlation_id: str | None = None
