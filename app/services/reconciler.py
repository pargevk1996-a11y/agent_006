"""Follow-up on steps that were paid for but never reported back.

Waiting for a generation is not guaranteed to succeed: the poll can hit
``POLL_TIMEOUT_SECONDS``, the webhook can be lost, the process can be killed
mid-wait. In every one of those cases the platform was already charged and is
still working — so a step left as "failed by timeout" is a *wrong report about
spent money*, not just a missing link.

This sweeper closes that gap. It periodically asks the platform about steps that
were launched, never settled, and belong to a job that has already ended, then
rewrites the step and its job with the real outcome. It never launches anything
and never spends: the only calls it makes are status reads.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from app.clients.base import VibeClient
from app.clients.exceptions import VibeAPIError, VibeError
from app.domain.plan import Job, JobStep, StepStatus, utcnow
from app.repositories.jobs import JobRepository, StepExecutionRecord
from app.services.executor import TERMINAL_STATUSES, apply_outcome, terminal_status_of

logger = logging.getLogger(__name__)


class Reconciler:
    def __init__(
        self,
        *,
        client: VibeClient,
        job_repo: JobRepository,
        interval_seconds: float = 300.0,
        min_age_seconds: float = 60.0,
    ) -> None:
        self.client = client
        self.jobs = job_repo
        self.interval_seconds = interval_seconds
        self.min_age_seconds = min_age_seconds
        self._task: asyncio.Task[None] | None = None

    @property
    def is_running(self) -> bool:
        return self._task is not None

    async def start(self) -> None:
        if self.interval_seconds <= 0:
            logger.info("reconciler_disabled", extra={"reason": "RECONCILE_INTERVAL_SECONDS=0"})
            return
        if self._task is None:
            self._task = asyncio.create_task(self._loop(), name="reconciler")
            logger.info(
                "reconciler_started",
                extra={"interval": self.interval_seconds, "min_age": self.min_age_seconds},
            )

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def sweep(self) -> int:
        """Settle everything that can be settled right now; returns how many."""
        cutoff = datetime.now(UTC) - timedelta(seconds=self.min_age_seconds)
        records = await self.jobs.stale_steps(updated_before=cutoff.isoformat())
        settled = 0
        for record in records:
            try:
                if await self._reconcile(record):
                    settled += 1
            except VibeError as exc:
                # One unreachable generation must not stop the rest of the sweep.
                logger.warning(
                    "reconcile_step_unavailable",
                    extra={"step_id": record.step_id, "generation_id": record.generation_id,
                           "detail": str(exc)},
                )
        return settled

    async def _reconcile(self, record: StepExecutionRecord) -> bool:
        job = await self.jobs.get(record.job_id)
        if job is None:  # pragma: no cover - defensive
            return False
        step = next((s for s in job.steps if s.step_id == record.step_id), None)
        if step is None or step.generation_id is None:  # pragma: no cover - defensive
            return False

        payload = await self._final_status(step)
        if payload is None:
            return False  # still running upstream — ask again next sweep

        previous_status = step.status
        previous_cost = step.actual_cost_rub
        step.actual_cost_rub = apply_outcome(step, payload)
        self._rewrite_job(job, step, previous_status=previous_status)

        await self.jobs.save(job)
        await self.jobs.update_step(
            plan_id=job.plan_id,
            step_id=step.step_id,
            status=step.status,
            generation_id=step.generation_id,
            actual_cost_rub=step.actual_cost_rub,
        )
        logger.info(
            "step_reconciled",
            extra={
                "job_id": job.job_id,
                "step_id": step.step_id,
                "generation_id": step.generation_id,
                "step_status": step.status.value,
                "job_status": job.status.value,
                "cost_before_rub": previous_cost,
                "actual_cost_rub": step.actual_cost_rub,
            },
        )
        return True

    async def _final_status(self, step: JobStep) -> dict[str, Any] | None:
        """The platform's verdict, or ``None`` while it is still working."""
        try:
            if step.is_long_voiceover:
                status_fn = getattr(self.client, "voiceover_status", None)
                if status_fn is None:  # pragma: no cover - client without the endpoint
                    return None
                payload = await status_fn(step.generation_id)
            else:
                payload = await self.client.generation_status(step.generation_id)
        except VibeAPIError as exc:
            if exc.status_code == 404:
                # The platform does not know this generation; nothing more will ever
                # arrive, so settle it instead of asking forever.
                return {
                    "status": "error",
                    "error_message": (
                        f"Платформа не знает генерацию {step.generation_id} "
                        "(404 при досмотре) — результат получить невозможно."
                    ),
                    "cost": step.actual_cost_rub,
                }
            raise
        return payload if str(payload.get("status", "")).lower() in TERMINAL_STATUSES else None

    def _rewrite_job(self, job: Job, step: JobStep, *, previous_status: StepStatus) -> None:
        """Replace what the job said about this step with what actually happened."""
        # The timeout complaint recorded earlier is no longer the truth about the step.
        job.errors = [e for e in job.errors if not e.startswith(f"{step.step_id}: ")]
        if step.status is StepStatus.FAILED:
            job.errors.append(f"{step.step_id}: {step.error}")
        job.warnings.append(
            f"«{step.step_id}»: результат получен досмотром после «{previous_status.value}» — "
            f"шаг перезаписан как «{step.status.value}», списано "
            f"{step.actual_cost_rub:.2f}₽."
        )
        job.actual_cost_rub = round(sum(s.actual_cost_rub for s in job.steps), 4)

        final = terminal_status_of(job)
        if final is not None:
            job.status = final
            job.finished_at = job.finished_at or utcnow()

    async def _loop(self) -> None:
        while True:
            await asyncio.sleep(self.interval_seconds)
            try:
                settled = await self.sweep()
                if settled:
                    logger.info("reconcile_sweep", extra={"settled_steps": settled})
            except asyncio.CancelledError:
                raise
            except Exception:  # a bad sweep must not kill the loop
                logger.exception("reconcile_sweep_failed")
