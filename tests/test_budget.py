"""The budget must never be exceeded — the single most important guarantee."""

from __future__ import annotations

import pytest

from app.core.errors import BudgetExceededError, ValidationError
from app.domain.plan import AccountSnapshot, JobStatus, StepKind
from app.services.budget import BudgetGuard
from tests.conftest import make_brief


class TestBudgetGuard:
    def test_commit_within_budget(self):
        guard = BudgetGuard(budget_rub=100.0)
        guard.commit(40.0)
        assert guard.remaining_rub == 60.0

    def test_commit_over_budget_raises_and_does_not_charge(self):
        guard = BudgetGuard(budget_rub=100.0)
        guard.commit(90.0)
        with pytest.raises(BudgetExceededError):
            guard.commit(20.0)
        assert guard.committed_rub == 90.0  # unchanged

    def test_safety_margin_shrinks_spendable_envelope(self):
        guard = BudgetGuard(budget_rub=100.0, safety_margin=0.1)
        assert guard.spendable_rub == 90.0
        with pytest.raises(BudgetExceededError, match="резерв"):
            guard.check(95.0)

    def test_unknown_balance_blocks_spending(self):
        guard = BudgetGuard(budget_rub=100.0, account=AccountSnapshot(balance_rub=None))
        with pytest.raises(BudgetExceededError, match="fail-closed"):
            guard.check_account(10.0)

    def test_missing_account_blocks_spending(self):
        guard = BudgetGuard(budget_rub=100.0, account=None)
        with pytest.raises(BudgetExceededError, match="fail-closed"):
            guard.check_account(10.0)

    def test_balance_below_cost_blocks(self):
        guard = BudgetGuard(budget_rub=1000.0, account=AccountSnapshot(balance_rub=5.0))
        with pytest.raises(BudgetExceededError, match="баланс"):
            guard.check_account(10.0)

    def test_daily_limit_blocks(self):
        guard = BudgetGuard(
            budget_rub=1000.0,
            account=AccountSnapshot(
                balance_rub=1000.0, daily_limit_rub=100.0, daily_spent_rub=95.0
            ),
        )
        with pytest.raises(BudgetExceededError, match="дневной лимит"):
            guard.check_account(10.0)

    def test_refund_returns_budget(self):
        guard = BudgetGuard(budget_rub=100.0)
        guard.commit(40.0)
        guard.release(40.0)
        assert guard.remaining_rub == 100.0


class TestPlanNeverExceedsBudget:
    async def test_plan_total_stays_within_budget(self, service):
        brief = make_brief(formats=["image", "voice", "video", "music"], budget_rub=60.0)
        plan = await service.create_plan(brief)
        assert plan.total_estimated_rub <= plan.budget_rub
        assert plan.budget_remaining_rub >= 0

    async def test_tiny_budget_drops_expensive_formats(self, service):
        brief = make_brief(formats=["image", "video"], budget_rub=20.0)
        plan = await service.create_plan(brief)
        chosen = {s.format.value for s in plan.steps}
        assert "video" not in chosen, "видео дороже 20₽ и должно быть исключено"
        assert plan.total_estimated_rub <= 20.0
        assert any("video" in w for w in plan.warnings)

    async def test_budget_too_small_for_anything_is_infeasible(self, service):
        plan = await service.create_plan(make_brief(formats=["video"], budget_rub=1.0))
        assert plan.status.value == "infeasible"
        assert plan.steps == []

    async def test_execution_never_spends_more_than_budget(self, service, client):
        brief = make_brief(formats=["image", "voice"], budget_rub=45.0)
        plan = await service.create_plan(brief)
        job = await service.execute_plan(plan.plan_id, confirmed=True)
        assert job.actual_cost_rub <= brief.budget_rub
        assert client.balance_rub == 5000.0 - job.actual_cost_rub

    async def test_budget_above_service_ceiling_is_rejected(self, service, settings):
        with pytest.raises(ValidationError, match="MAX_BUDGET_RUB"):
            await service.create_plan(
                make_brief(budget_rub=settings.max_budget_rub + 1)
            )

    async def test_insufficient_account_balance_aborts_before_spending(
        self, service, client
    ):
        client.balance_rub = 5.0
        plan = await service.create_plan(make_brief(formats=["image"], budget_rub=500.0))
        job = await service.execute_plan(plan.plan_id, confirmed=True)
        assert job.status is JobStatus.ABORTED
        assert job.actual_cost_rub == 0.0
        assert client.balance_rub == 5.0
        assert any("баланс" in e for e in job.errors)

    async def test_local_text_step_is_free_and_never_dropped(self, service):
        plan = await service.create_plan(make_brief(formats=["text"], budget_rub=1.0))
        assert len(plan.steps) == 1
        step = plan.steps[0]
        assert step.kind is StepKind.LOCAL
        assert step.estimated_cost_rub == 0.0
        assert step.local_output
