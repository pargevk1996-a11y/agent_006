"""Long voiceover: prompts above a model's single-request limit.

Per the catalog (``el-tts-turbo``): a prompt over ``prompt_max`` (up to
``long_voiceover_prompt_max``) is chunked, voiced and merged, and ``/generate``
answers with ``voiceover_id`` + ``status_url`` instead of ``generation_id``.
Polling then goes to ``GET /voiceover/long/{id}``.
"""

from __future__ import annotations

from app.clients.mock import load_capabilities_fixture
from app.domain.capabilities import Capabilities
from app.domain.plan import JobStatus, StepStatus
from tests.conftest import make_brief

LONG_SCRIPT = "Текст для длинной озвучки продукта. " * 400  # ~14 000 символов


class TestCatalogContract:
    def test_only_models_with_a_long_limit_advertise_it(self):
        caps = Capabilities.parse(load_capabilities_fixture())
        supporting = [s.key for s in caps.models_for("voice") if s.supports_long_voiceover]
        assert supporting, "в каталоге должна быть хотя бы одна модель с длинной озвучкой"
        for spec in caps.models_for("voice"):
            if spec.supports_long_voiceover:
                assert spec.effective_prompt_max > spec.prompt_max
            else:
                assert spec.effective_prompt_max == spec.prompt_max


class TestPlanning:
    async def test_long_script_selects_a_model_that_can_take_it(self, service):
        plan = await service.create_plan(
            make_brief(formats=["voice"], budget_rub=5000.0, voiceover_script=LONG_SCRIPT)
        )
        step = plan.steps[0]
        caps = Capabilities.parse(load_capabilities_fixture())
        spec = caps.get("voice", step.model)
        assert spec.supports_long_voiceover
        assert step.params["prompt"] == LONG_SCRIPT.strip(), "сценарий не должен быть обрезан"
        assert len(step.params["prompt"]) > (spec.prompt_max or 0)

    async def test_models_that_would_truncate_the_script_are_rejected(self, service):
        plan = await service.create_plan(
            make_brief(formats=["voice"], budget_rub=5000.0, voiceover_script=LONG_SCRIPT)
        )
        rejected = {r.model: r.reason for r in plan.steps[0].rejected_alternatives}
        assert any("обрезала бы сценарий" in reason for reason in rejected.values())

    async def test_long_script_is_priced_by_its_length(self, service):
        short = await service.create_plan(
            make_brief(formats=["voice"], budget_rub=5000.0, voiceover_script="Короткий текст.")
        )
        long = await service.create_plan(
            make_brief(formats=["voice"], budget_rub=5000.0, voiceover_script=LONG_SCRIPT)
        )
        assert long.total_estimated_rub > short.total_estimated_rub
        assert long.total_estimated_rub <= long.budget_rub

    async def test_long_voiceover_beyond_budget_is_dropped_not_truncated(self, service):
        plan = await service.create_plan(
            make_brief(formats=["voice"], budget_rub=50.0, voiceover_script=LONG_SCRIPT)
        )
        assert plan.steps == [], "не влезло в бюджет — шаг исключается, а не режется"
        assert plan.status.value == "infeasible"

    def test_auto_composed_script_is_trimmed_but_an_explicit_one_is_not(self):
        """Trimming our own draft is fine; trimming the user's script is not.

        Uses a synthetic catalog with a tiny limit, so the rule is tested directly
        instead of depending on the current limits of live models.
        """
        from app.domain.policy import Policy
        from app.services.planner import Planner

        payload = load_capabilities_fixture()
        payload["models"]["voice"] = {
            "tiny-tts": {"price": 5, "required": ["prompt"], "limits": {"prompt_max": 120}}
        }
        planner = Planner(Capabilities.parse(payload), Policy.load(None))

        auto = planner.build(
            make_brief(formats=["voice"], budget_rub=500.0), plan_id="p", spendable_rub=500.0
        )
        assert auto.steps, "наш собственный черновик можно ужать"
        assert len(auto.steps[0].params["prompt"]) <= 120
        assert any("усечён" in w for w in auto.steps[0].warnings)

        explicit = planner.build(
            make_brief(formats=["voice"], budget_rub=500.0, voiceover_script=LONG_SCRIPT),
            plan_id="p",
            spendable_rub=500.0,
        )
        assert explicit.steps == [], "чужой сценарий резать нельзя — модель отклоняется"


class TestExecution:
    async def test_long_voiceover_is_polled_on_its_own_endpoint(self, service, client):
        plan = await service.create_plan(
            make_brief(formats=["voice"], budget_rub=5000.0, voiceover_script=LONG_SCRIPT)
        )
        job = await service.execute_plan(plan.plan_id, confirmed=True)

        step = job.steps[0]
        assert job.status is JobStatus.SUCCEEDED
        assert step.is_long_voiceover
        assert step.status is StepStatus.SUCCEEDED
        assert step.display_url
        assert client.calls_of("voiceover_status"), "опрос должен идти в /voiceover/long/{id}"
        assert not client.calls_of("generation_status")
        assert job.actual_cost_rub <= plan.budget_rub

    async def test_failed_long_voiceover_is_refunded(self, service, client):
        plan = await service.create_plan(
            make_brief(formats=["voice"], budget_rub=5000.0, voiceover_script=LONG_SCRIPT)
        )
        client.fail_models = {plan.steps[0].model}

        job = await service.execute_plan(plan.plan_id, confirmed=True)

        step = job.steps[0]
        assert step.status is StepStatus.FAILED
        assert step.refunded
        assert step.actual_cost_rub == 0.0
        assert job.actual_cost_rub == 0.0

    async def test_repeat_execute_does_not_recharge_a_long_voiceover(self, service, client):
        plan = await service.create_plan(
            make_brief(formats=["voice"], budget_rub=5000.0, voiceover_script=LONG_SCRIPT)
        )
        first = await service.execute_plan(plan.plan_id, confirmed=True)
        balance = client.balance_rub

        second = await service.execute_plan(plan.plan_id, confirmed=True)
        assert second.job_id == first.job_id
        assert client.balance_rub == balance
        assert len(client.calls_of("generate")) == 1
