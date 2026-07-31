"""Aggregates for the spend dashboard.

Read straight from the tables rather than from in-process counters: for a service
whose whole job is being trusted with money, "сколько потрачено" must survive a
restart and must not drift from what the reports say. The database is already the
source of truth for every ruble; the metrics endpoint just sums it up.

The per-job money lives inside the JSON document, so the sums use SQLite's JSON1
functions instead of duplicating those numbers into columns that could disagree.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.repositories.db import Database


@dataclass(frozen=True)
class MetricsSnapshot:
    plans_by_status: dict[str, int] = field(default_factory=dict)
    jobs_by_status: dict[str, int] = field(default_factory=dict)
    steps_by_status: dict[str, int] = field(default_factory=dict)
    plan_estimated_rub: float = 0.0
    job_estimated_rub: float = 0.0
    job_actual_rub: float = 0.0
    refunded_steps: int = 0
    idempotency_keys: int = 0
    media_uploads: int = 0
    webhook_events: int = 0

    @property
    def plans_total(self) -> int:
        return sum(self.plans_by_status.values())

    @property
    def jobs_total(self) -> int:
        return sum(self.jobs_by_status.values())


class MetricsRepository:
    def __init__(self, db: Database) -> None:
        self.db = db

    async def snapshot(self) -> MetricsSnapshot:
        plans = await self.db.fetch_all(
            "SELECT status, COUNT(*) AS n, COALESCE(SUM(total_estimated_rub), 0) AS rub "
            "FROM plans GROUP BY status"
        )
        jobs = await self.db.fetch_all(
            """
            SELECT status,
                   COUNT(*) AS n,
                   COALESCE(SUM(json_extract(document, '$.estimated_cost_rub')), 0) AS est,
                   COALESCE(SUM(json_extract(document, '$.actual_cost_rub')), 0) AS act
              FROM jobs
             GROUP BY status
            """
        )
        steps = await self.db.fetch_all(
            "SELECT status, COUNT(*) AS n FROM step_executions GROUP BY status"
        )
        # A refunded step is money that came back — invisible in the cost sums, and
        # exactly what tells a provider problem apart from our own bad planning.
        refunded = await self.db.fetch_one(
            """
            SELECT COUNT(*) AS n
              FROM jobs, json_each(json_extract(jobs.document, '$.steps'))
             WHERE json_extract(value, '$.refunded') = 1
            """
        )
        keys = await self.db.fetch_one("SELECT COUNT(*) AS n FROM api_idempotency")
        uploads = await self.db.fetch_one("SELECT COUNT(*) AS n FROM media_uploads")
        hooks = await self.db.fetch_one("SELECT COUNT(*) AS n FROM webhook_events")

        return MetricsSnapshot(
            plans_by_status={row["status"]: row["n"] for row in plans},
            jobs_by_status={row["status"]: row["n"] for row in jobs},
            steps_by_status={row["status"]: row["n"] for row in steps},
            plan_estimated_rub=round(sum(row["rub"] for row in plans), 4),
            job_estimated_rub=round(sum(row["est"] for row in jobs), 4),
            job_actual_rub=round(sum(row["act"] for row in jobs), 4),
            refunded_steps=refunded["n"] if refunded else 0,
            idempotency_keys=keys["n"] if keys else 0,
            media_uploads=uploads["n"] if uploads else 0,
            webhook_events=hooks["n"] if hooks else 0,
        )
