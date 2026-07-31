"""Signed callbacks from the platform.

The signature is verified against the **raw** request bytes before the body is
parsed. An unsigned or wrongly signed webhook is rejected with 401 and changes
nothing — a forged callback must never be able to mark a step as paid-and-done.

With asynchronous execution this is also a completion path: when a callback
settles the last outstanding step, the job is finished here, without waiting for
an executor loop to notice.
"""

from __future__ import annotations

import json
import logging

from fastapi import APIRouter, Depends, Header, Request

from app.api.deps import get_job_repository, get_settings
from app.api.schemas import WebhookAck
from app.core.config import Settings
from app.core.errors import ValidationError
from app.domain.plan import StepStatus, utcnow
from app.repositories.jobs import JobRepository
from app.services.executor import terminal_status_of
from app.services.webhooks import verify_webhook

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/webhooks", tags=["webhooks"])

SUCCESS_STATUSES = {"complete", "completed", "success"}


@router.post(
    "/vibe",
    response_model=WebhookAck,
    summary="Callback платформы (обязательная проверка HMAC-подписи)",
)
async def vibe_webhook(
    request: Request,
    x_vibe_signature: str | None = Header(default=None, alias="X-Vibe-Signature"),
    x_vibe_event: str | None = Header(default=None, alias="X-Vibe-Event"),
    settings: Settings = Depends(get_settings),
    jobs: JobRepository = Depends(get_job_repository),
) -> WebhookAck:
    raw_body = await request.body()

    verify_webhook(
        raw_body,
        x_vibe_signature,
        secret=settings.webhook_secret_value,
        api_token=settings.token_value,
        allow_legacy=settings.vibe_webhook_legacy_fallback,
    )

    try:
        payload = json.loads(raw_body)
    except ValueError as exc:
        raise ValidationError("Тело вебхука не является корректным JSON.") from exc
    if not isinstance(payload, dict):
        raise ValidationError("Тело вебхука должно быть JSON-объектом.")

    generation_id = payload.get("generation_id")
    generation_id = int(generation_id) if isinstance(generation_id, (int, str)) and str(
        generation_id
    ).isdigit() else None
    event = str(payload.get("event") or x_vibe_event or "unknown")
    await jobs.record_webhook(generation_id, event, payload)

    matched_step: str | None = None
    if generation_id is not None:
        found = await jobs.find_by_generation(generation_id)
        if found:
            job, step_id = found
            matched_step = step_id
            _apply(job, step_id, payload)
            await jobs.save(job)
            await jobs.update_step(
                plan_id=job.plan_id,
                step_id=step_id,
                status=_step_status(payload),
                generation_id=generation_id,
                actual_cost_rub=_cost(payload),
            )

    logger.info(
        "webhook_received",
        extra={"event": event, "generation_id": generation_id, "matched_step": matched_step},
    )
    return WebhookAck(status="accepted", generation_id=generation_id, matched_step=matched_step)


def _step_status(payload: dict) -> StepStatus:
    state = str(payload.get("status") or "").lower()
    return StepStatus.SUCCEEDED if state in SUCCESS_STATUSES else StepStatus.FAILED


def _cost(payload: dict) -> float:
    if payload.get("refunded"):
        return 0.0
    cost = payload.get("cost")
    return float(cost) if isinstance(cost, (int, float)) else 0.0


def _apply(job, step_id: str, payload: dict) -> None:
    step = next((s for s in job.steps if s.step_id == step_id), None)
    if step is None or step.status is StepStatus.SUCCEEDED:
        return
    step.status = _step_status(payload)
    step.refunded = bool(payload.get("refunded"))
    step.actual_cost_rub = _cost(payload)
    step.result_url = payload.get("result_url") or step.result_url
    step.display_url = payload.get("display_url") or step.display_url
    if step.status is StepStatus.FAILED:
        step.error = str(payload.get("error_message") or "Генерация завершилась ошибкой.")

    job.actual_cost_rub = round(sum(s.actual_cost_rub for s in job.steps), 4)

    # Only conclude the job when nothing is outstanding: a callback for step 1 of 3
    # says nothing about the job as a whole.
    final = terminal_status_of(job)
    if final is not None and not job.status.is_terminal:
        job.status = final
        job.finished_at = job.finished_at or utcnow()
