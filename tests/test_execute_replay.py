"""Calling execute twice must never charge twice."""

from __future__ import annotations

import pytest

from app.core.config import AppMode
from app.core.errors import (
    ConfirmationRequiredError,
    ConflictError,
    ModeNotAllowedError,
    NotFoundError,
)
from app.domain.plan import JobStatus, StepStatus
from tests.conftest import make_brief, make_service, make_settings


class TestConfirmationGate:
    async def test_execute_without_confirmation_spends_nothing(self, service, client):
        plan = await service.create_plan(make_brief(formats=["image"], budget_rub=500.0))
        with pytest.raises(ConfirmationRequiredError):
            await service.execute_plan(plan.plan_id, confirmed=False)
        assert client.calls_of("generate") == []
        assert client.balance_rub == 5000.0

    async def test_unknown_plan_is_404(self, service):
        with pytest.raises(NotFoundError):
            await service.execute_plan("no-such-plan", confirmed=True)

    async def test_infeasible_plan_cannot_be_executed(self, service, client):
        plan = await service.create_plan(make_brief(formats=["video"], budget_rub=1.0))
        with pytest.raises(ConflictError):
            await service.execute_plan(plan.plan_id, confirmed=True)
        assert client.calls_of("generate") == []

    async def test_estimate_mode_refuses_execution(self, client, database, tmp_path):
        settings = make_settings(
            tmp_path, app_mode=AppMode.ESTIMATE, vibe_api_token="oc_test_token_value"
        )
        service = make_service(client, settings, database)
        plan = await service.create_plan(make_brief(formats=["image"], budget_rub=500.0))
        with pytest.raises(ModeNotAllowedError, match="estimate"):
            await service.execute_plan(plan.plan_id, confirmed=True)
        assert client.calls_of("generate") == []


class TestExecuteReplay:
    async def test_second_execute_returns_the_same_job(self, service, client):
        plan = await service.create_plan(
            make_brief(formats=["image", "voice"], budget_rub=500.0)
        )
        first = await service.execute_plan(plan.plan_id, confirmed=True)
        generate_calls = len(client.calls_of("generate"))
        balance = client.balance_rub

        second = await service.execute_plan(plan.plan_id, confirmed=True)

        assert second.job_id == first.job_id
        assert second.actual_cost_rub == first.actual_cost_rub
        assert len(client.calls_of("generate")) == generate_calls
        assert client.balance_rub == balance

    async def test_replay_after_abort_does_not_leak_charges(self, service, client):
        plan = await service.create_plan(make_brief(formats=["image"], budget_rub=500.0))
        client.price_multiplier = 2.0
        aborted = await service.execute_plan(plan.plan_id, confirmed=True)
        assert aborted.status is JobStatus.ABORTED

        again = await service.execute_plan(plan.plan_id, confirmed=True)
        assert again.actual_cost_rub == 0.0
        assert client.calls_of("generate") == []
        assert client.balance_rub == 5000.0

    async def test_ledger_prevents_relaunch_of_a_finished_step(self, service, client, database):
        """Even if a job is re-run, an already-launched step is only awaited, not repaid."""
        from app.domain.plan import Job, JobStep
        from app.repositories.jobs import JobRepository
        from app.services.budget import BudgetGuard
        from app.services.executor import Executor

        plan = await service.create_plan(make_brief(formats=["image"], budget_rub=500.0))
        job = await service.execute_plan(plan.plan_id, confirmed=True)
        charged = client.balance_rub
        assert job.status is JobStatus.SUCCEEDED

        # Simulate a crash-and-retry: a brand-new job document over the same plan.
        repo = JobRepository(database)
        retry_job = Job(
            job_id="retry-job",
            plan_id=plan.plan_id,
            mode=plan.mode,
            budget_rub=plan.budget_rub,
            estimated_cost_rub=plan.total_estimated_rub,
            steps=[
                JobStep(
                    step_id=s.step_id,
                    format=s.format,
                    kind=s.kind,
                    type=s.type,
                    model=s.model,
                    idempotency_key=s.idempotency_key,
                    estimated_cost_rub=s.estimated_cost_rub,
                )
                for s in plan.steps
            ],
        )
        await repo.save(retry_job)
        executor = Executor(client=client, job_repo=repo, poll_interval=0.001, poll_timeout=1.0)
        result = await executor.run(
            plan, retry_job, BudgetGuard(budget_rub=plan.budget_rub, account=plan.account)
        )

        assert len(client.calls_of("generate")) == 1, "повторного списания быть не должно"
        assert client.balance_rub == charged
        assert result.steps[0].status is StepStatus.SUCCEEDED
        assert any("уже была запущена" in w for w in result.warnings)
