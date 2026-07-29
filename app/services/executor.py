"""Plan execution — the only place in the codebase that can spend money.

Invariants:

* every billable call carries ``strict: true`` and a stable ``idempotency_key``;
* a step is claimed in the ledger *before* it is launched, so a crash between
  charge and bookkeeping cannot cause a second charge on retry;
* the budget guard is consulted before every launch and updated after it;
* if a guard trips mid-plan, remaining steps are skipped rather than attempted.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from typing import Any

from app.clients.base import VibeClient
from app.clients.exceptions import VibeAPIError, VibeError, VibePollTimeoutError
from app.core.errors import BudgetExceededError
from app.domain.plan import (
    Job,
    JobStatus,
    JobStep,
    Plan,
    PlanStep,
    StepKind,
    StepStatus,
)
from app.repositories.jobs import JobRepository
from app.services.budget import BudgetGuard

logger = logging.getLogger(__name__)

SUCCESS_STATUSES = {"complete", "completed", "success"}


class Executor:
    def __init__(
        self,
        *,
        client: VibeClient,
        job_repo: JobRepository,
        callback_url: str | None = None,
        poll_interval: float = 10.0,
        poll_timeout: float = 900.0,
    ) -> None:
        self.client = client
        self.job_repo = job_repo
        self.callback_url = callback_url
        self.poll_interval = poll_interval
        self.poll_timeout = poll_timeout

    async def run(self, plan: Plan, job: Job, guard: BudgetGuard) -> Job:
        job.status = JobStatus.RUNNING
        job.started_at = datetime.now(UTC)
        await self.job_repo.save(job)

        aborted = False
        for plan_step in plan.steps:
            job_step = _find_step(job, plan_step.step_id)
            if job_step is None:
                continue
            if aborted:
                job_step.status = StepStatus.SKIPPED
                job_step.error = "Пропущен: исполнение остановлено предыдущей ошибкой бюджета."
                continue
            if job_step.status is StepStatus.SUCCEEDED:
                continue  # already done in an earlier run of this job

            try:
                await self._run_step(plan, plan_step, job, job_step, guard)
            except BudgetExceededError as exc:
                job_step.status = StepStatus.SKIPPED
                job_step.error = str(exc)
                job.errors.append(f"{plan_step.step_id}: {exc}")
                job.warnings.append(
                    "Исполнение остановлено бюджетным гардом — оставшиеся шаги не запускались."
                )
                aborted = True
            except VibeError as exc:
                job_step.status = StepStatus.FAILED
                job_step.error = _describe(exc)
                job_step.finished_at = datetime.now(UTC)
                job.errors.append(f"{plan_step.step_id}: {job_step.error}")
                await self.job_repo.update_step(
                    plan_id=plan.plan_id, step_id=plan_step.step_id, status=StepStatus.FAILED
                )
                if isinstance(exc, VibeAPIError) and exc.is_budget_related:
                    job.warnings.append(
                        "Платформа сообщила о нехватке средств/лимита — дальнейшие шаги отменены."
                    )
                    aborted = True
            finally:
                job.actual_cost_rub = round(
                    sum(s.actual_cost_rub for s in job.steps), 4
                )
                await self.job_repo.save(job)

        job.status = _aggregate_status(job, aborted=aborted)
        job.finished_at = datetime.now(UTC)
        job.actual_cost_rub = round(sum(s.actual_cost_rub for s in job.steps), 4)
        await self.job_repo.save(job)
        logger.info(
            "job_finished",
            extra={
                "job_id": job.job_id,
                "plan_id": plan.plan_id,
                "job_status": job.status.value,
                "actual_cost_rub": job.actual_cost_rub,
                "estimated_cost_rub": job.estimated_cost_rub,
                "duration_seconds": job.duration_seconds,
            },
        )
        return job

    # -- single step -------------------------------------------------------
    async def _run_step(
        self,
        plan: Plan,
        plan_step: PlanStep,
        job: Job,
        job_step: JobStep,
        guard: BudgetGuard,
    ) -> None:
        job_step.status = StepStatus.RUNNING
        job_step.started_at = datetime.now(UTC)
        job_step.attempts += 1

        if plan_step.kind is StepKind.LOCAL:
            job_step.local_output = plan_step.local_output
            job_step.status = StepStatus.SUCCEEDED
            job_step.actual_cost_rub = 0.0
            job_step.finished_at = datetime.now(UTC)
            await self.job_repo.update_step(
                plan_id=plan.plan_id,
                step_id=plan_step.step_id,
                status=StepStatus.SUCCEEDED,
                actual_cost_rub=0.0,
            )
            return

        body = plan_step.generate_body(callback_url=self.callback_url)
        record = await self.job_repo.claim_step(
            plan_id=plan.plan_id,
            step_id=plan_step.step_id,
            job_id=job.job_id,
            idempotency_key=plan_step.idempotency_key,
            body=body,
        )

        if record.generation_id is not None:
            # Already launched (earlier run / crash after charge): never pay twice.
            job_step.generation_id = record.generation_id
            job.warnings.append(
                f"{plan_step.step_id}: генерация {record.generation_id} уже была запущена — "
                "повторное списание не выполняется, агент только дожидается результата."
            )
        else:
            guard.check_all(plan_step.estimated_cost_rub, label=f"шаг {plan_step.step_id}")
            logger.info(
                "generate_start",
                extra={
                    "step_id": plan_step.step_id,
                    "model": plan_step.model,
                    "type": plan_step.type,
                    "estimated_cost_rub": plan_step.estimated_cost_rub,
                    "idempotency_key": plan_step.idempotency_key,
                },
            )
            response = await self.client.generate(body)
            job_step.generation_id = _generation_id_of(response)
            job_step.task_id = response.get("task_id")
            # Prompts over the model's single-request limit are processed as a long
            # voiceover: /generate answers with voiceover_id and a separate status URL.
            job_step.is_long_voiceover = bool(response.get("long_voiceover"))
            # Text models answer synchronously: the copy is in the /generate reply and
            # is NOT repeated by /generation/{id}/status — capture it right here.
            job_step.text_output = _text_of(response) or job_step.text_output
            charged = _as_float(response.get("cost"), plan_step.estimated_cost_rub)
            job_step.actual_cost_rub = charged
            await self.job_repo.update_step(
                plan_id=plan.plan_id,
                step_id=plan_step.step_id,
                status=StepStatus.RUNNING,
                generation_id=job_step.generation_id,
                actual_cost_rub=charged,
            )
            try:
                guard.commit(charged, label=f"шаг {plan_step.step_id}")
            except BudgetExceededError as exc:
                # Already charged upstream: record it honestly and stop the plan.
                guard.committed_rub = round(guard.committed_rub + charged, 4)
                job.warnings.append(
                    f"{plan_step.step_id}: фактическое списание {charged:.2f}₽ выше плановых "
                    f"{plan_step.estimated_cost_rub:.2f}₽ — план остановлен. {exc}"
                )
                raise

        if job_step.is_long_voiceover:
            await self._await_long_voiceover(plan, plan_step, job, job_step, guard)
            return

        if job_step.generation_id is None:
            if _is_terminal(response):
                # Synchronous generation (type=text): the result is already here.
                await self._settle(plan, plan_step, job, job_step, guard, response)
                return
            job_step.status = StepStatus.FAILED
            job_step.error = "Платформа не вернула generation_id."
            job_step.finished_at = datetime.now(UTC)
            job.errors.append(f"{plan_step.step_id}: {job_step.error}")
            return

        await self._await_result(plan, plan_step, job, job_step, guard)

    async def _await_long_voiceover(
        self,
        plan: Plan,
        plan_step: PlanStep,
        job: Job,
        job_step: JobStep,
        guard: BudgetGuard,
    ) -> None:
        """Poll GET /voiceover/long/{id} — a different endpoint with the same contract."""
        status_fn = getattr(self.client, "voiceover_status", None)
        if status_fn is None:
            job_step.status = StepStatus.FAILED
            job_step.error = "Клиент не умеет опрашивать длинную озвучку."
            job_step.finished_at = datetime.now(UTC)
            job.errors.append(f"{plan_step.step_id}: {job_step.error}")
            return

        waited = 0.0
        while True:
            payload = await status_fn(job_step.generation_id)
            state = str(payload.get("status", "")).lower()
            if state in SUCCESS_STATUSES | {"error", "failed", "cancelled"}:
                await self._settle(plan, plan_step, job, job_step, guard, payload)
                return
            if waited >= self.poll_timeout:
                job_step.status = StepStatus.FAILED
                job_step.error = (
                    f"Таймаут ожидания длинной озвучки ({waited:.0f}с). Склейка продолжается "
                    f"на стороне платформы: GET /voiceover/long/{job_step.generation_id}."
                )
                job_step.finished_at = datetime.now(UTC)
                job.errors.append(f"{plan_step.step_id}: {job_step.error}")
                return
            await asyncio.sleep(self.poll_interval)
            waited += self.poll_interval

    async def _await_result(
        self,
        plan: Plan,
        plan_step: PlanStep,
        job: Job,
        job_step: JobStep,
        guard: BudgetGuard,
    ) -> None:
        try:
            status_payload = await _wait_for(
                self.client,
                job_step.generation_id,
                interval=self.poll_interval,
                timeout=self.poll_timeout,
            )
        except VibePollTimeoutError as exc:
            job_step.status = StepStatus.FAILED
            job_step.error = (
                f"Таймаут ожидания результата ({exc.waited:.0f}с). Генерация продолжается "
                "на стороне платформы; итог придёт вебхуком или через GET /generation/"
                f"{job_step.generation_id}/status."
            )
            job_step.finished_at = datetime.now(UTC)
            job.errors.append(f"{plan_step.step_id}: {job_step.error}")
            return

        await self._settle(plan, plan_step, job, job_step, guard, status_payload)

    async def _settle(
        self,
        plan: Plan,
        plan_step: PlanStep,
        job: Job,
        job_step: JobStep,
        guard: BudgetGuard,
        status_payload: dict[str, Any],
    ) -> None:
        """Record the terminal outcome of a step and reconcile the money spent."""
        state = str(status_payload.get("status", "")).lower()
        refunded = bool(status_payload.get("refunded"))
        final_cost = _as_float(status_payload.get("cost"), job_step.actual_cost_rub)
        job_step.refunded = refunded
        job_step.finished_at = datetime.now(UTC)

        if state in SUCCESS_STATUSES:
            job_step.status = StepStatus.SUCCEEDED
            job_step.result_url = status_payload.get("result_url")
            job_step.display_url = status_payload.get("display_url") or status_payload.get(
                "file_url"
            )
            job_step.text_output = _text_of(status_payload) or job_step.text_output
            if final_cost != job_step.actual_cost_rub:
                guard.release(job_step.actual_cost_rub, label=f"пересчёт {plan_step.step_id}")
                guard.committed_rub = round(guard.committed_rub + final_cost, 4)
                job_step.actual_cost_rub = final_cost
        else:
            job_step.status = StepStatus.FAILED
            job_step.error = str(
                status_payload.get("error_message") or "Генерация завершилась ошибкой."
            )
            job.errors.append(f"{plan_step.step_id}: {job_step.error}")
            if refunded or final_cost == 0:
                guard.release(job_step.actual_cost_rub, label=f"возврат {plan_step.step_id}")
                job_step.actual_cost_rub = 0.0

        await self.job_repo.update_step(
            plan_id=plan.plan_id,
            step_id=plan_step.step_id,
            status=job_step.status,
            generation_id=job_step.generation_id,
            actual_cost_rub=job_step.actual_cost_rub,
        )


async def _wait_for(
    client: VibeClient,
    generation_id: int | str | None,
    *,
    interval: float,
    timeout: float,  # noqa: ASYNC109 - polling budget, not a cancel scope
) -> dict[str, Any]:
    waiter = getattr(client, "wait_for_generation", None)
    if waiter is not None:
        return await waiter(generation_id, interval=interval, timeout=timeout)

    # Protocol-only clients (e.g. the mock) expose just the status endpoint.
    waited = 0.0
    while True:
        payload = await client.generation_status(generation_id)  # type: ignore[arg-type]
        state = str(payload.get("status", "")).lower()
        if state in SUCCESS_STATUSES | {"error", "failed", "cancelled"}:
            return payload
        if waited >= timeout:
            raise VibePollTimeoutError(generation_id or "?", waited)
        await asyncio.sleep(interval)
        waited += interval


def _text_of(payload: dict[str, Any]) -> str | None:
    """Text produced by a text model, wherever the platform put it."""
    for key in ("text", "output", "result_text", "content", "answer"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return None


def _is_terminal(payload: dict[str, Any]) -> bool:
    """True when a /generate reply already carries the final outcome."""
    state = str(payload.get("status", "")).lower()
    if state in SUCCESS_STATUSES | {"error", "failed", "cancelled"}:
        return True
    return _text_of(payload) is not None


def _find_step(job: Job, step_id: str) -> JobStep | None:
    return next((s for s in job.steps if s.step_id == step_id), None)


def _generation_id_of(response: dict[str, Any]) -> int | None:
    for key in ("generation_id", "id", "voiceover_id"):
        value = response.get(key)
        if isinstance(value, int):
            return value
        if isinstance(value, str) and value.isdigit():
            return int(value)
    return None


def _as_float(value: Any, default: float) -> float:
    return float(value) if isinstance(value, (int, float)) else default


def _describe(error: Exception) -> str:
    if isinstance(error, VibeAPIError):
        parts = [f"{error.code}: {error.message}"]
        if error.request_id:
            parts.append(f"request_id={error.request_id}")
        return " | ".join(parts)
    return str(error)


def _aggregate_status(job: Job, *, aborted: bool) -> JobStatus:
    statuses = [s.status for s in job.steps]
    succeeded = sum(1 for s in statuses if s is StepStatus.SUCCEEDED)
    failed = sum(1 for s in statuses if s is StepStatus.FAILED)
    skipped = sum(1 for s in statuses if s is StepStatus.SKIPPED)
    if aborted:
        return JobStatus.ABORTED if succeeded == 0 else JobStatus.PARTIAL
    if failed == 0 and skipped == 0:
        return JobStatus.SUCCEEDED
    if succeeded == 0:
        return JobStatus.FAILED
    return JobStatus.PARTIAL
