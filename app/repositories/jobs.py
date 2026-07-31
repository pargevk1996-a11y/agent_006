"""Job and step-execution persistence.

``step_executions`` is the anti-double-spend ledger: before a step is launched we
claim its ``(plan_id, step_id)`` row; the row carries the idempotency key and a
hash of the exact request body. A repeat of the same step must present the same
hash and reuses the same key; anything else is refused.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from typing import Any

from app.domain.plan import Job, JobStatus, StepStatus
from app.repositories.db import Database, dumps


def request_hash(body: dict[str, Any]) -> str:
    payload = dumps({k: v for k, v in sorted(body.items()) if k != "callback_url"})
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]


class StepExecutionRecord:
    __slots__ = (
        "actual_cost_rub",
        "generation_id",
        "idempotency_key",
        "job_id",
        "plan_id",
        "request_hash",
        "status",
        "step_id",
    )

    def __init__(self, row: Any) -> None:
        self.idempotency_key: str = row["idempotency_key"]
        self.plan_id: str = row["plan_id"]
        self.step_id: str = row["step_id"]
        self.job_id: str = row["job_id"]
        self.request_hash: str = row["request_hash"]
        self.generation_id: int | None = row["generation_id"]
        self.status: str = row["status"]
        self.actual_cost_rub: float = row["actual_cost_rub"]


class JobRepository:
    def __init__(self, db: Database) -> None:
        self.db = db

    # -- jobs ---------------------------------------------------------------
    async def save(self, job: Job) -> None:
        await self.db.execute(
            """
            INSERT INTO jobs (job_id, plan_id, status, created_at, document)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(job_id) DO UPDATE SET
                status = excluded.status,
                document = excluded.document
            """,
            (
                job.job_id,
                job.plan_id,
                job.status.value,
                job.created_at.isoformat(),
                job.model_dump_json(),
            ),
        )

    async def get(self, job_id: str) -> Job | None:
        row = await self.db.fetch_one("SELECT document FROM jobs WHERE job_id = ?", (job_id,))
        return Job.model_validate_json(row["document"]) if row else None

    async def get_by_plan(self, plan_id: str) -> Job | None:
        row = await self.db.fetch_one(
            "SELECT document FROM jobs WHERE plan_id = ? ORDER BY created_at DESC LIMIT 1",
            (plan_id,),
        )
        return Job.model_validate_json(row["document"]) if row else None

    async def claim_for_execution(self, job_id: str) -> bool:
        """Take ownership of a queued job. ``False`` means somebody else has it.

        This conditional ``UPDATE`` is the lock that keeps async execution safe:
        a duplicate enqueue, a second worker or a restart-recovery pass can all
        target the same job, and only the one that flips ``queued → running``
        proceeds. The status lives in both the column and the JSON document, so
        they are moved together in a single statement.
        """
        changed = await self.db.execute(
            """
            UPDATE jobs
               SET status = ?,
                   document = json_set(document, '$.status', ?, '$.started_at', ?)
             WHERE job_id = ? AND status = ?
            """,
            (
                JobStatus.RUNNING.value,
                JobStatus.RUNNING.value,
                datetime.now(UTC).isoformat(),
                job_id,
                JobStatus.QUEUED.value,
            ),
        )
        return changed == 1

    async def requeue(self, job_id: str) -> None:
        """Put a job back in line — used to resume work after a restart."""
        await self.db.execute(
            """
            UPDATE jobs
               SET status = ?,
                   document = json_set(document, '$.status', ?)
             WHERE job_id = ?
            """,
            (JobStatus.QUEUED.value, JobStatus.QUEUED.value, job_id),
        )

    async def unfinished_job_ids(self) -> list[str]:
        """Jobs a previous process left mid-flight, oldest first."""
        rows = await self.db.fetch_all(
            "SELECT job_id FROM jobs WHERE status IN (?, ?) ORDER BY created_at",
            (JobStatus.QUEUED.value, JobStatus.RUNNING.value),
        )
        return [row["job_id"] for row in rows]

    async def find_by_generation(self, generation_id: int) -> tuple[Job, str] | None:
        """Locate the job and step a webhook refers to."""
        row = await self.db.fetch_one(
            "SELECT job_id, step_id FROM step_executions WHERE generation_id = ?",
            (generation_id,),
        )
        if row is None:
            return None
        job = await self.get(row["job_id"])
        return (job, row["step_id"]) if job else None

    # -- step ledger --------------------------------------------------------
    async def claim_step(
        self,
        *,
        plan_id: str,
        step_id: str,
        job_id: str,
        idempotency_key: str,
        body: dict[str, Any],
    ) -> StepExecutionRecord:
        """Reserve the step, or return the existing reservation.

        Raises :class:`ValueError` when the same step is retried with a different
        payload or a different key — that would break idempotency guarantees.
        """
        digest = request_hash(body)
        existing = await self.get_step(plan_id, step_id)
        if existing is not None:
            if existing.idempotency_key != idempotency_key:
                raise ValueError(
                    f"step {step_id} of plan {plan_id} was already claimed with a different "
                    "idempotency key — refusing to launch it again"
                )
            if existing.request_hash != digest:
                raise ValueError(
                    f"step {step_id} of plan {plan_id} changed its request body — "
                    "a repeat must reuse the identical payload"
                )
            return existing

        await self.db.execute(
            """
            INSERT INTO step_executions (idempotency_key, plan_id, step_id, job_id,
                                         request_hash, generation_id, status,
                                         actual_cost_rub, updated_at)
            VALUES (?, ?, ?, ?, ?, NULL, ?, 0, ?)
            """,
            (
                idempotency_key,
                plan_id,
                step_id,
                job_id,
                digest,
                StepStatus.PENDING.value,
                datetime.now(UTC).isoformat(),
            ),
        )
        record = await self.get_step(plan_id, step_id)
        assert record is not None
        return record

    async def get_step(self, plan_id: str, step_id: str) -> StepExecutionRecord | None:
        row = await self.db.fetch_one(
            "SELECT * FROM step_executions WHERE plan_id = ? AND step_id = ?",
            (plan_id, step_id),
        )
        return StepExecutionRecord(row) if row else None

    async def update_step(
        self,
        *,
        plan_id: str,
        step_id: str,
        status: StepStatus,
        generation_id: int | None = None,
        actual_cost_rub: float | None = None,
    ) -> None:
        await self.db.execute(
            """
            UPDATE step_executions
               SET status = ?,
                   generation_id = COALESCE(?, generation_id),
                   actual_cost_rub = COALESCE(?, actual_cost_rub),
                   updated_at = ?
             WHERE plan_id = ? AND step_id = ?
            """,
            (
                status.value,
                generation_id,
                actual_cost_rub,
                datetime.now(UTC).isoformat(),
                plan_id,
                step_id,
            ),
        )

    # -- webhook audit trail -------------------------------------------------
    async def record_webhook(self, generation_id: int | None, event: str, payload: dict) -> None:
        await self.db.execute(
            "INSERT INTO webhook_events (generation_id, event, received_at, payload) "
            "VALUES (?, ?, ?, ?)",
            (generation_id, event, datetime.now(UTC).isoformat(), dumps(payload)),
        )
