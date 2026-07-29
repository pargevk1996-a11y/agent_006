"""Token-billed text models (claude-opus-5, gpt-5.6-sol).

These have no price in ``/capabilities`` — they are billed per actual tokens. The
only pre-flight ceiling is the reserve ``/generate/estimate`` reports as
``balance.current - balance.after_reserve``. The budget guard must use that
reserve, and a step that cannot be priced at all must never be executed.
"""

from __future__ import annotations

import pytest

from app.domain.capabilities import Capabilities, ModelSpec, price_bounds
from app.domain.plan import JobStatus, StepKind
from app.services.plan_service import estimated_cost_of
from tests.conftest import make_brief


class TestReserveExtraction:
    def test_fixed_price_is_used_directly(self):
        cost, kind = estimated_cost_of({"estimated_cost_rub": 16.5})
        assert cost == 16.5
        assert kind == "estimated_cost_rub"

    def test_reserve_is_derived_from_balance(self):
        cost, kind = estimated_cost_of(
            {"estimated_cost_rub": None, "balance": {"current": 600, "after_reserve": 593.92}}
        )
        assert cost == pytest.approx(6.08)
        assert "max_tokens" in kind

    def test_zero_cost_is_a_valid_price(self):
        assert estimated_cost_of({"estimated_cost_rub": 0})[0] == 0.0

    def test_missing_price_and_reserve_yields_none(self):
        assert estimated_cost_of({"estimated_cost_rub": None})[0] is None
        assert estimated_cost_of({"balance": {"current": 600}})[0] is None
        assert estimated_cost_of({})[0] is None

    def test_boolean_is_not_a_price(self):
        assert estimated_cost_of({"estimated_cost_rub": True})[0] is None

    def test_negative_reserve_is_rejected(self):
        payload = {"balance": {"current": 100, "after_reserve": 120}}
        assert estimated_cost_of(payload)[0] is None


class TestCatalogView:
    def test_token_billed_models_are_recognised(self):
        spec = ModelSpec.parse(
            "claude-opus-5",
            "text",
            {"required": ["prompt"], "optional": ["system", "max_tokens", "effort"]},
        )
        assert spec.is_token_billed
        bounds = price_bounds(spec)
        assert not bounds.known
        assert "токен" in bounds.basis

    def test_fixed_price_model_is_not_token_billed(self):
        spec = ModelSpec.parse("z-image", "image", {"price": 1.2, "required": ["prompt"]})
        assert not spec.is_token_billed

    def test_live_catalog_snapshot_has_text_models(self):
        from app.clients.mock import load_capabilities_fixture

        caps = Capabilities.parse(load_capabilities_fixture())
        text_models = caps.models_by_type.get("text", {})
        assert text_models, "слепок /capabilities должен содержать модели типа text"
        assert all(spec.is_token_billed for spec in text_models.values())


class TestPlanningWithTokenBilledModels:
    async def test_text_step_gets_its_price_from_the_reserve(self, service):
        plan = await service.create_plan(make_brief(formats=["text"], budget_rub=200.0))
        step = plan.steps[0]
        assert step.kind is StepKind.GENERATION
        assert step.cost_source == "estimate"
        assert step.estimated_cost_rub > 0
        assert "резерв" in step.cost_basis
        assert plan.total_estimated_rub <= plan.budget_rub

    async def test_budget_too_small_falls_back_to_free_local_copy(self, service, client):
        plan = await service.create_plan(make_brief(formats=["text"], budget_rub=1.0))
        step = plan.steps[0]
        assert step.kind is StepKind.LOCAL
        assert step.estimated_cost_rub == 0.0
        assert step.local_output
        assert any("локально" in w for w in plan.warnings)

        job = await service.execute_plan(plan.plan_id, confirmed=True)
        assert job.status is JobStatus.SUCCEEDED
        assert job.actual_cost_rub == 0.0
        assert client.calls_of("generate") == []

    async def test_actual_charge_never_exceeds_the_reserve(self, service, client):
        plan = await service.create_plan(make_brief(formats=["text"], budget_rub=200.0))
        reserved = plan.steps[0].estimated_cost_rub

        job = await service.execute_plan(plan.plan_id, confirmed=True)

        step = job.steps[0]
        assert job.status is JobStatus.SUCCEEDED
        assert 0 < step.actual_cost_rub <= reserved, "списание не может превысить резерв"
        assert step.text_output, "текстовая модель должна вернуть текст"
        assert job.actual_cost_rub <= plan.budget_rub

    async def test_unpriceable_step_is_dropped_not_executed(self, service, client, monkeypatch):
        """If neither a price nor a reserve comes back, the step must not run."""
        original = client.estimate

        async def priceless(body):
            payload = await original(body)
            if body.get("type") == "text":
                return {**payload, "estimated_cost_rub": None, "balance": {"current": 600}}
            return payload

        monkeypatch.setattr(client, "estimate", priceless)
        plan = await service.create_plan(make_brief(formats=["text"], budget_rub=200.0))

        assert plan.steps[0].kind is StepKind.LOCAL, "шаг без цены заменяется бесплатным локальным"
        assert any("fail-closed" in w or "стоимость" in w for w in plan.warnings)

        job = await service.execute_plan(plan.plan_id, confirmed=True)
        assert job.actual_cost_rub == 0.0
        assert client.calls_of("generate") == []

    async def test_max_tokens_is_always_sent(self, service, client):
        await service.create_plan(make_brief(formats=["text"], budget_rub=200.0))
        text_estimates = [b for b in client.calls_of("estimate") if b.get("type") == "text"]
        assert text_estimates
        for body in text_estimates:
            assert body["max_tokens"] > 0
            assert body["strict"] is True

    async def test_bigger_max_tokens_means_bigger_reserve(self, client):
        small = await client.estimate(
            {"type": "text", "model": "claude-opus-5", "prompt": "x", "max_tokens": 500}
        )
        big = await client.estimate(
            {"type": "text", "model": "claude-opus-5", "prompt": "x", "max_tokens": 4000}
        )
        assert estimated_cost_of(small)[0] < estimated_cost_of(big)[0]


class TestReplanWithExactPrices:
    async def test_step_is_downgraded_not_dropped(self, service, client):
        """A tight budget must cost the plan quality, not a whole deliverable.

        The catalog cannot price the text model, so the first pass over-commits the
        image budget. Once the real text price arrives, the plan is rebuilt with a
        cheaper image model instead of losing the image step.
        """
        # 22 ₽ is deliberately between "cheapest image fits" and "preferred image fits".
        plan = await service.create_plan(
            make_brief(formats=["text", "image"], budget_rub=22.0)
        )
        formats = {s.format.value for s in plan.steps}
        assert formats == {"text", "image"}, "оба формата должны остаться в плане"
        assert plan.total_estimated_rub <= plan.spendable_rub
        assert any("пересобран" in w for w in plan.warnings)

        image = next(s for s in plan.steps if s.format.value == "image")
        assert image.kind is StepKind.GENERATION
        assert image.estimated_cost_rub > 0

    async def test_no_replan_when_the_plan_already_fits(self, service, client):
        await service.create_plan(make_brief(formats=["text", "image"], budget_rub=500.0))
        estimates = len(client.calls_of("estimate"))
        assert estimates == 2, "лишних оценок при укладывающемся плане быть не должно"

    async def test_replan_never_exceeds_the_budget(self, service):
        for budget in (8.0, 12.0, 25.0, 40.0, 120.0):
            plan = await service.create_plan(
                make_brief(formats=["text", "image", "voice"], budget_rub=budget)
            )
            assert plan.total_estimated_rub <= plan.spendable_rub, f"бюджет {budget}₽ нарушен"


class TestSynchronousTextResult:
    async def test_copy_is_captured_from_the_generate_reply(self, service, client):
        """Live returns the text in POST /generate; status afterwards has output: null."""
        plan = await service.create_plan(make_brief(formats=["text"], budget_rub=200.0))
        job = await service.execute_plan(plan.plan_id, confirmed=True)

        step = job.steps[0]
        assert step.status.value == "succeeded"
        assert step.text_output, "текст должен быть сохранён из ответа /generate"
        assert step.generation_id

        # The status endpoint no longer carries the copy — we must not lose it.
        status = await client.generation_status(step.generation_id)
        assert status.get("output") is None
