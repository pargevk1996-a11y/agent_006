"""A failing step must not sink the whole plan — nor hide its cost."""

from __future__ import annotations

from app.clients.exceptions import VibeAPIError
from app.domain.plan import JobStatus, StepStatus
from tests.conftest import make_brief


class TestPartialFailure:
    async def test_one_failing_generation_yields_partial_job(self, service, client):
        plan = await service.create_plan(
            make_brief(formats=["image", "voice"], budget_rub=500.0)
        )
        failing_model = plan.steps[0].model
        client.fail_models = {failing_model}

        job = await service.execute_plan(plan.plan_id, confirmed=True)

        assert job.status is JobStatus.PARTIAL
        failed = [s for s in job.steps if s.status is StepStatus.FAILED]
        succeeded = [s for s in job.steps if s.status is StepStatus.SUCCEEDED]
        assert len(failed) == 1 and len(succeeded) == 1
        assert failed[0].error
        assert failed[0].refunded is True
        assert failed[0].actual_cost_rub == 0.0, "возвращённые средства не считаются тратой"
        assert job.actual_cost_rub == succeeded[0].actual_cost_rub
        assert job.errors

    async def test_all_steps_failing_yields_failed_job(self, service, client):
        plan = await service.create_plan(
            make_brief(formats=["image", "voice"], budget_rub=500.0)
        )
        client.fail_models = {s.model for s in plan.steps if s.model}

        job = await service.execute_plan(plan.plan_id, confirmed=True)

        assert job.status is JobStatus.FAILED
        assert job.actual_cost_rub == 0.0

    async def test_successful_steps_keep_their_links(self, service, client):
        plan = await service.create_plan(
            make_brief(formats=["text", "image", "voice"], budget_rub=500.0)
        )
        client.fail_models = {plan.steps[1].model}

        job = await service.execute_plan(plan.plan_id, confirmed=True)

        voice = next(s for s in job.steps if s.format.value == "voice")
        text = next(s for s in job.steps if s.format.value == "text")
        assert voice.display_url and voice.display_url.startswith("https://")
        assert text.local_output and text.actual_cost_rub == 0.0

    async def test_insufficient_balance_midway_stops_remaining_steps(self, service, client):
        plan = await service.create_plan(
            make_brief(formats=["image", "voice", "video"], budget_rub=500.0)
        )
        # Enough for the first step only; the account then cannot pay for the rest.
        client.balance_rub = plan.steps[0].estimated_cost_rub

        job = await service.execute_plan(plan.plan_id, confirmed=True)

        assert job.status in {JobStatus.ABORTED, JobStatus.PARTIAL}
        assert job.actual_cost_rub <= plan.budget_rub
        assert client.balance_rub >= 0

    async def test_upstream_4xx_marks_step_failed_without_retry(self, service, client, monkeypatch):
        plan = await service.create_plan(make_brief(formats=["image"], budget_rub=500.0))
        calls = {"n": 0}

        async def rejecting_generate(_body):
            calls["n"] += 1
            raise VibeAPIError(422, code="validation_failed", message="плохой параметр")

        monkeypatch.setattr(client, "generate", rejecting_generate)
        job = await service.execute_plan(plan.plan_id, confirmed=True)

        assert calls["n"] == 1
        assert job.status is JobStatus.FAILED
        assert job.actual_cost_rub == 0.0
        assert "validation_failed" in job.steps[0].error

    async def test_poll_timeout_is_reported_not_silently_lost(self, service, client, settings):
        client.complete_after_polls = 10_000  # never finishes
        settings.poll_interval_seconds = 0.001
        settings.poll_timeout_seconds = 0.005

        plan = await service.create_plan(make_brief(formats=["image"], budget_rub=500.0))
        job = await service.execute_plan(plan.plan_id, confirmed=True)

        step = job.steps[0]
        assert step.status is StepStatus.FAILED
        assert "Таймаут" in step.error
        # The money was really spent, so the report must show it honestly.
        assert step.actual_cost_rub > 0
        assert job.actual_cost_rub == step.actual_cost_rub
