"""Plan persistence."""

from __future__ import annotations

from app.domain.plan import Plan
from app.repositories.db import Database, dumps


class PlanRepository:
    def __init__(self, db: Database) -> None:
        self.db = db

    async def save(self, plan: Plan) -> None:
        await self.db.execute(
            """
            INSERT INTO plans (plan_id, created_at, mode, status, budget_rub,
                               total_estimated_rub, job_id, document)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(plan_id) DO UPDATE SET
                status = excluded.status,
                total_estimated_rub = excluded.total_estimated_rub,
                job_id = excluded.job_id,
                document = excluded.document
            """,
            (
                plan.plan_id,
                plan.created_at.isoformat(),
                plan.mode,
                plan.status.value,
                plan.budget_rub,
                plan.total_estimated_rub,
                plan.job_id,
                plan.model_dump_json(),
            ),
        )

    async def get(self, plan_id: str) -> Plan | None:
        row = await self.db.fetch_one(
            "SELECT document FROM plans WHERE plan_id = ?", (plan_id,)
        )
        return Plan.model_validate_json(row["document"]) if row else None

    async def list_recent(self, limit: int = 20) -> list[Plan]:
        rows = await self.db.fetch_all(
            "SELECT document FROM plans ORDER BY created_at DESC LIMIT ?", (limit,)
        )
        return [Plan.model_validate_json(row["document"]) for row in rows]

    async def attach_job(self, plan_id: str, job_id: str, status: str) -> None:
        await self.db.execute(
            "UPDATE plans SET job_id = ?, status = ? WHERE plan_id = ?",
            (job_id, status, plan_id),
        )


__all__ = ["PlanRepository", "dumps"]
