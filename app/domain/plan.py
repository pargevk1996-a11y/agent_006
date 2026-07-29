"""Plan / job domain models — the persisted contract between planning and execution."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field

from app.domain.brief import Brief, ContentFormat


def utcnow() -> datetime:
    return datetime.now(UTC)


class PlanStatus(StrEnum):
    READY = "ready"            # every requested format got a step
    DEGRADED = "degraded"      # some formats dropped/downgraded to fit the budget
    INFEASIBLE = "infeasible"  # nothing affordable — execution is refused
    EXECUTED = "executed"      # an execution job exists for this plan


class StepKind(StrEnum):
    GENERATION = "generation"  # billable POST /generate
    LOCAL = "local"            # produced locally, always free (e.g. copywriting)


class StepStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    SKIPPED = "skipped"


class JobStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    PARTIAL = "partial"
    FAILED = "failed"
    ABORTED = "aborted"  # stopped before/while spending because a guard tripped

    @property
    def is_terminal(self) -> bool:
        return self in {
            JobStatus.SUCCEEDED,
            JobStatus.PARTIAL,
            JobStatus.FAILED,
            JobStatus.ABORTED,
        }


class RejectedCandidate(BaseModel):
    model: str
    reason: str


class PlanStep(BaseModel):
    """One unit of work with its own price, parameters and idempotency key."""

    step_id: str
    format: ContentFormat
    kind: StepKind = StepKind.GENERATION
    type: str | None = None           # Agent API `type` (image/video/voice/music)
    model: str | None = None
    model_description: str = ""
    params: dict[str, Any] = Field(default_factory=dict)
    idempotency_key: str
    estimated_cost_rub: float = 0.0
    cost_source: str = "capabilities"  # capabilities | estimate | local
    cost_basis: str = ""
    reason: str = ""
    rejected_alternatives: list[RejectedCandidate] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    local_output: str | None = None

    def generate_body(self, *, callback_url: str | None = None) -> dict[str, Any]:
        """Exact ``POST /generate`` body: strict mode + stable idempotency key."""
        body: dict[str, Any] = {
            "type": self.type,
            "model": self.model,
            **self.params,
            "strict": True,
            "idempotency_key": self.idempotency_key,
        }
        if callback_url:
            body["callback_url"] = callback_url
        return body

    def estimate_body(self) -> dict[str, Any]:
        """``POST /generate/estimate`` body — same shape, no idempotency/callback."""
        return {"type": self.type, "model": self.model, **self.params, "strict": True}


class AccountSnapshot(BaseModel):
    balance_rub: float | None = None
    daily_limit_rub: float | None = None
    daily_spent_rub: float | None = None
    source: str = "mock"

    @property
    def daily_remaining_rub(self) -> float | None:
        if self.daily_limit_rub is None:
            return None
        return max(0.0, self.daily_limit_rub - (self.daily_spent_rub or 0.0))


class Plan(BaseModel):
    plan_id: str
    created_at: datetime = Field(default_factory=utcnow)
    mode: str
    status: PlanStatus
    brief: Brief
    budget_rub: float
    safety_margin_rub: float = 0.0
    spendable_rub: float = 0.0
    total_estimated_rub: float = 0.0
    steps: list[PlanStep] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    account: AccountSnapshot = Field(default_factory=AccountSnapshot)
    job_id: str | None = None
    capabilities_fingerprint: str = ""

    @property
    def budget_remaining_rub(self) -> float:
        return round(self.budget_rub - self.total_estimated_rub, 4)

    @property
    def billable_steps(self) -> list[PlanStep]:
        return [s for s in self.steps if s.kind is StepKind.GENERATION]


class JobStep(BaseModel):
    step_id: str
    format: ContentFormat
    kind: StepKind
    type: str | None = None
    model: str | None = None
    status: StepStatus = StepStatus.PENDING
    idempotency_key: str
    generation_id: int | None = None
    task_id: str | None = None
    estimated_cost_rub: float = 0.0
    actual_cost_rub: float = 0.0
    refunded: bool = False
    result_url: str | None = None
    display_url: str | None = None
    local_output: str | None = None
    error: str | None = None
    attempts: int = 0
    started_at: datetime | None = None
    finished_at: datetime | None = None

    @property
    def duration_seconds(self) -> float | None:
        if self.started_at and self.finished_at:
            return round((self.finished_at - self.started_at).total_seconds(), 3)
        return None


class Job(BaseModel):
    job_id: str
    plan_id: str
    mode: str
    status: JobStatus = JobStatus.PENDING
    budget_rub: float = 0.0
    estimated_cost_rub: float = 0.0
    actual_cost_rub: float = 0.0
    steps: list[JobStep] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=utcnow)
    started_at: datetime | None = None
    finished_at: datetime | None = None

    @property
    def duration_seconds(self) -> float | None:
        if self.started_at and self.finished_at:
            return round((self.finished_at - self.started_at).total_seconds(), 3)
        return None

    @property
    def budget_remaining_rub(self) -> float:
        return round(self.budget_rub - self.actual_cost_rub, 4)
