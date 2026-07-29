"""A price that moves between plan and execute must stop the execution."""

from __future__ import annotations

from app.clients.exceptions import VibeNetworkError
from app.domain.plan import JobStatus, StepStatus
from tests.conftest import make_brief


class TestPriceDrift:
    async def test_price_increase_aborts_without_spending(self, service, client):
        plan = await service.create_plan(make_brief(formats=["image"], budget_rub=500.0))
        balance_before = client.balance_rub

        client.price_multiplier = 1.5  # upstream raised prices after planning

        job = await service.execute_plan(plan.plan_id, confirmed=True)

        assert job.status is JobStatus.ABORTED
        assert job.actual_cost_rub == 0.0
        assert client.balance_rub == balance_before
        assert client.calls_of("generate") == []
        assert any("цена изменилась" in e for e in job.errors)
        assert all(s.status is StepStatus.SKIPPED for s in job.steps)

    async def test_tiny_rounding_drift_does_not_block(self, service, client):
        plan = await service.create_plan(make_brief(formats=["image"], budget_rub=500.0))
        step_cost = plan.steps[0].estimated_cost_rub
        client.price_multiplier = 1 + (0.005 / step_cost)  # +0.005₽, below tolerance

        job = await service.execute_plan(plan.plan_id, confirmed=True)
        assert job.status is JobStatus.SUCCEEDED

    async def test_price_drop_is_accepted_and_reported(self, service, client):
        plan = await service.create_plan(make_brief(formats=["image"], budget_rub=500.0))
        planned = plan.steps[0].estimated_cost_rub
        client.price_multiplier = 0.5

        job = await service.execute_plan(plan.plan_id, confirmed=True)

        assert job.status is JobStatus.SUCCEEDED
        assert job.actual_cost_rub < planned
        assert any("цена снизилась" in w for w in job.warnings)

    async def test_price_increase_beyond_budget_is_blocked_even_within_balance(
        self, service, client
    ):
        plan = await service.create_plan(make_brief(formats=["image"], budget_rub=20.0))
        client.price_multiplier = 3.0  # 16.5 -> 49.5, still affordable for the account

        job = await service.execute_plan(plan.plan_id, confirmed=True)

        assert job.status is JobStatus.ABORTED
        assert job.actual_cost_rub == 0.0
        assert client.calls_of("generate") == []

    async def test_estimate_failure_at_execute_is_fail_closed(self, service, client, monkeypatch):
        """If we cannot re-price a step, we refuse to spend on it."""
        plan = await service.create_plan(make_brief(formats=["image"], budget_rub=500.0))

        async def unreachable(_body):
            raise VibeNetworkError("upstream unreachable")

        monkeypatch.setattr(client, "estimate", unreachable)
        job = await service.execute_plan(plan.plan_id, confirmed=True)

        assert job.status is JobStatus.ABORTED
        assert job.actual_cost_rub == 0.0
        assert client.calls_of("generate") == []
        assert any("fail-closed" in e for e in job.errors)
