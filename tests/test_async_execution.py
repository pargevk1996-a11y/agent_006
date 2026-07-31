"""Asynchronous execution: accept fast, spend in the background, never twice.

The guarantees under test are the ones that only appear once execution stops
being a synchronous call: a job is claimed before it runs, a restart resumes it
instead of re-paying for it, and a broken job does not take the worker down.
"""

from __future__ import annotations

import asyncio

from app.domain.plan import JobStatus, StepStatus
from app.repositories.db import Database
from app.repositories.jobs import JobRepository
from app.services.job_queue import JobQueue
from tests.conftest import execute_and_wait, make_brief, make_service, make_settings, wait_for_job

BRIEF = {
    "product_name": "CRM для мастеров маникюра",
    "product_description": "Онлайн-запись, напоминания и учёт расходников.",
    "target_audience": "Мастера маникюра, 22–40 лет",
    "offer": "Первый месяц бесплатно",
    "formats": ["text", "image"],
    "budget_rub": 300,
}


class TestExecuteIsAccepted:
    async def test_execute_returns_202_with_a_queued_job(self, api_client):
        plan = (await api_client.post("/api/v1/plans", json=BRIEF)).json()
        response = await api_client.post(
            f"/api/v1/plans/{plan['plan_id']}/execute", json={"confirmed": True}
        )

        assert response.status_code == 202
        job = response.json()
        assert job["status"] == "queued"
        assert job["actual_cost_rub"] == 0.0, "принятие плана не должно ничего списывать"
        assert response.headers["Location"] == f"/api/v1/jobs/{job['job_id']}"

    async def test_queued_job_finishes_in_the_background(self, api_client):
        plan = (await api_client.post("/api/v1/plans", json=BRIEF)).json()
        job = await execute_and_wait(api_client, plan["plan_id"])

        assert job["status"] == "succeeded"
        assert job["actual_cost_rub"] <= job["budget_rub"]
        assert job["finished_at"] is not None
        for step in job["steps"]:
            assert step["status"] == "succeeded"

    async def test_wait_true_returns_the_finished_job_inline(self, api_client):
        plan = (await api_client.post("/api/v1/plans", json=BRIEF)).json()
        response = await api_client.post(
            f"/api/v1/plans/{plan['plan_id']}/execute",
            json={"confirmed": True, "wait": True},
        )

        assert response.status_code == 200
        assert response.json()["status"] == "succeeded"

    async def test_gates_still_answer_synchronously(self, api_client):
        """A refusal must not be discovered later by polling."""
        plan = (await api_client.post("/api/v1/plans", json=BRIEF)).json()
        response = await api_client.post(
            f"/api/v1/plans/{plan['plan_id']}/execute", json={"confirmed": False}
        )
        assert response.status_code == 400
        assert response.json()["error"] == "confirmation_required"

    async def test_second_execute_does_not_create_a_rival_job(self, api_client):
        plan = (await api_client.post("/api/v1/plans", json=BRIEF)).json()
        first = (
            await api_client.post(
                f"/api/v1/plans/{plan['plan_id']}/execute", json={"confirmed": True}
            )
        ).json()
        second = (
            await api_client.post(
                f"/api/v1/plans/{plan['plan_id']}/execute", json={"confirmed": True}
            )
        ).json()

        assert second["job_id"] == first["job_id"]
        finished = await wait_for_job(api_client, first["job_id"])
        assert finished["actual_cost_rub"] <= finished["budget_rub"]

    async def test_health_reports_the_worker_pool(self, api_client):
        body = (await api_client.get("/health")).json()
        assert body["executor_workers"] >= 1
        assert body["queue_depth"] == 0


class TestJobClaim:
    """The database row is the lock; two workers on one job must not both spend."""

    async def test_only_one_worker_executes_a_job(self, client, settings, database):
        service = make_service(client, settings, database, queue=_idle_queue())
        plan = await service.create_plan(make_brief(formats=["image"], budget_rub=500.0))
        job = await service.execute_plan(plan.plan_id, confirmed=True)
        assert job.status is JobStatus.QUEUED

        results = await asyncio.gather(
            service.run_job(job.job_id), service.run_job(job.job_id)
        )

        assert len(client.calls_of("generate")) == 1, "второй воркер не должен списывать"
        assert client.balance_rub == 5000.0 - results[0].actual_cost_rub
        assert {r.job_id for r in results} == {job.job_id}

    async def test_claim_is_refused_for_a_finished_job(self, service, client, database):
        plan = await service.create_plan(make_brief(formats=["image"], budget_rub=500.0))
        job = await service.execute_plan(plan.plan_id, confirmed=True, wait=True)
        assert job.status is JobStatus.SUCCEEDED

        assert await JobRepository(database).claim_for_execution(job.job_id) is False
        replay = await service.run_job(job.job_id)
        assert replay.status is JobStatus.SUCCEEDED
        assert len(client.calls_of("generate")) == 1

    async def test_claim_moves_status_in_column_and_document(self, service, database):
        plan = await service.create_plan(make_brief(formats=["image"], budget_rub=500.0))
        job = await service.execute_plan(plan.plan_id, confirmed=True)  # inline: no queue
        repo = JobRepository(database)

        await repo.requeue(job.job_id)
        assert (await repo.get(job.job_id)).status is JobStatus.QUEUED
        row = await database.fetch_one(
            "SELECT status FROM jobs WHERE job_id = ?", (job.job_id,)
        )
        assert row["status"] == "queued"

        assert await repo.claim_for_execution(job.job_id) is True
        assert (await repo.get(job.job_id)).status is JobStatus.RUNNING


class TestRestartRecovery:
    async def test_resumed_job_is_not_recharged(self, service, client, database):
        plan = await service.create_plan(make_brief(formats=["image"], budget_rub=500.0))
        job = await service.execute_plan(plan.plan_id, confirmed=True, wait=True)
        spent = client.balance_rub

        # A restart puts the job back in line; the ledger already holds the
        # generation_id, so the resumed run must only look, never pay.
        await JobRepository(database).requeue(job.job_id)
        resumed = await service.run_job(job.job_id)

        assert len(client.calls_of("generate")) == 1
        assert client.balance_rub == spent
        assert resumed.actual_cost_rub == job.actual_cost_rub

    async def test_resumed_job_skips_the_price_recheck(self, service, client, database):
        """Money is already gone upstream — a price blip must not abort the job."""
        plan = await service.create_plan(make_brief(formats=["image"], budget_rub=500.0))
        job = await service.execute_plan(plan.plan_id, confirmed=True, wait=True)

        await JobRepository(database).requeue(job.job_id)
        client.price_multiplier = 2.0
        resumed = await service.run_job(job.job_id)

        assert resumed.status is JobStatus.SUCCEEDED
        assert any("возобновлено после перезапуска" in w for w in resumed.warnings)
        assert len(client.calls_of("generate")) == 1

    async def test_startup_requeues_a_job_left_behind(self, tmp_path, webhook_secret):
        """A process killed with a job in flight: the next start finishes it."""
        import httpx

        from app.clients.mock import MockVibeClient
        from app.main import create_app

        db_path = tmp_path / "recovery.db"
        settings = make_settings(
            tmp_path, db_path=db_path, vibe_webhook_secret=webhook_secret
        )
        database = Database(db_path)
        await database.connect()
        service = make_service(MockVibeClient(), settings, database, queue=_idle_queue())
        plan = await service.create_plan(make_brief(formats=["image"], budget_rub=500.0))
        job = await service.execute_plan(plan.plan_id, confirmed=True)
        assert job.status is JobStatus.QUEUED, "задание осталось в очереди, никто его не взял"
        await database.close()

        app = create_app(settings)
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as api:
                recovered = await wait_for_job(api, job.job_id)

        assert recovered["status"] == "succeeded"
        assert recovered["actual_cost_rub"] > 0


class TestAbortIsVisibleAsynchronously:
    async def test_price_drift_aborts_the_queued_job(self, client, settings, database):
        service = make_service(client, settings, database, queue=_idle_queue())
        plan = await service.create_plan(make_brief(formats=["image"], budget_rub=500.0))
        job = await service.execute_plan(plan.plan_id, confirmed=True)
        assert job.status is JobStatus.QUEUED

        client.price_multiplier = 2.0
        finished = await service.run_job(job.job_id)

        assert finished.job_id == job.job_id, "abort обновляет то же задание, а не создаёт новое"
        assert finished.status is JobStatus.ABORTED
        assert finished.actual_cost_rub == 0.0
        assert client.calls_of("generate") == []
        assert all(s.status is StepStatus.SKIPPED for s in finished.steps)
        assert finished.finished_at is not None

    async def test_aborted_plan_stays_re_runnable(self, client, settings, database):
        """An abort spends nothing, so the plan must not be marked executed."""
        from app.domain.plan import PlanStatus

        service = make_service(client, settings, database, queue=_idle_queue())
        plan = await service.create_plan(make_brief(formats=["image"], budget_rub=500.0))
        job = await service.execute_plan(plan.plan_id, confirmed=True)
        client.price_multiplier = 2.0
        await service.run_job(job.job_id)

        stored = await service.get_plan(plan.plan_id)
        assert stored.status is not PlanStatus.EXECUTED
        assert stored.job_id == job.job_id


class TestJobQueue:
    async def test_workers_survive_a_failing_job(self):
        seen: list[str] = []

        async def runner(job_id: str) -> None:
            seen.append(job_id)
            if job_id == "boom":
                raise RuntimeError("воркер должен пережить это")

        queue = JobQueue(runner, concurrency=1)
        await queue.start()
        await queue.enqueue("boom")
        await queue.enqueue("ok")
        await queue.drain()
        await queue.stop()

        assert seen == ["boom", "ok"]

    async def test_depth_reflects_pending_work(self):
        started = asyncio.Event()
        release = asyncio.Event()

        async def runner(job_id: str) -> None:
            started.set()
            await release.wait()

        queue = JobQueue(runner, concurrency=1)
        await queue.start()
        await queue.enqueue("first")
        await started.wait()
        await queue.enqueue("second")
        assert queue.depth == 1

        release.set()
        await queue.drain()
        await queue.stop()
        assert queue.depth == 0
        assert queue.is_running is False

    async def test_stop_is_idempotent(self):
        queue = JobQueue(_noop, concurrency=1)
        await queue.start()
        await queue.stop()
        await queue.stop()
        assert queue.is_running is False


async def _noop(job_id: str) -> None:  # pragma: no cover - placeholder runner
    return None


def _idle_queue() -> JobQueue:
    """A queue nobody drains: execute() enqueues, the test runs the job by hand."""
    return JobQueue(_noop, concurrency=1)
