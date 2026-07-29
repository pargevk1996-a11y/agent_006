"""Model selection: compatibility-driven, deterministic, explainable."""

from __future__ import annotations

import pytest

from app.clients.mock import load_capabilities_fixture
from app.domain.brief import SelectionStrategy
from app.domain.capabilities import Capabilities, ModelSpec, price_bounds
from app.domain.policy import Policy
from app.services.planner import Planner
from tests.conftest import make_brief


@pytest.fixture
def caps() -> Capabilities:
    return Capabilities.parse(load_capabilities_fixture())


@pytest.fixture
def planner(caps: Capabilities) -> Planner:
    return Planner(caps, Policy.load(None))


class TestCompatibility:
    def test_models_requiring_unavailable_media_are_rejected(self, planner):
        brief = make_brief(formats=["video"], budget_rub=5000.0)
        draft = planner.build(brief, plan_id="p", spendable_rub=5000.0)
        step = next(s for s in draft.steps if s.format.value == "video")

        assert step.model not in {"grok-itv", "motion-control-720p", "veed-avatar"}
        grok_itv = [r for r in step.rejected_alternatives if r.model == "grok-itv"]
        assert grok_itv and "image_urls" in grok_itv[0].reason

    def test_reference_image_unlocks_image_to_video_models(self, planner, caps):
        with_image = planner.build(
            make_brief(
                formats=["video"],
                budget_rub=5000.0,
                reference_image_urls=["https://lk.vibemarketolog.ru/uploads/a.png"],
            ),
            plan_id="p",
            spendable_rub=5000.0,
        )
        step = next(s for s in with_image.steps if s.format.value == "video")
        spec = caps.get("video", step.model)
        assert spec is not None
        # When the model accepts image inputs, the planner must actually supply them.
        if "image_urls" in spec.known_params:
            assert step.params.get("image_urls")

    def test_selected_params_are_a_subset_of_model_schema(self, planner, caps):
        brief = make_brief(formats=["image", "voice", "video", "music"], budget_rub=5000.0)
        draft = planner.build(brief, plan_id="p", spendable_rub=5000.0)
        for step in draft.steps:
            if step.model is None:
                continue
            spec = caps.get(step.type, step.model)
            assert spec is not None
            unknown = set(step.params) - spec.known_params
            assert not unknown, f"{step.model}: strict=true отвергнет {unknown}"
            assert not set(spec.required) - set(step.params)

    def test_enum_values_are_respected(self, planner, caps):
        brief = make_brief(formats=["video"], budget_rub=5000.0, aspect_ratio="21:9")
        draft = planner.build(brief, plan_id="p", spendable_rub=5000.0)
        step = next(s for s in draft.steps if s.format.value == "video")
        spec = caps.get("video", step.model)
        allowed = spec.allowed_values("aspect_ratio")
        if allowed and "aspect_ratio" in step.params:
            assert step.params["aspect_ratio"] in allowed

    def test_duration_is_clamped_to_model_limits(self, planner, caps):
        brief = make_brief(formats=["video"], budget_rub=5000.0, video_duration_seconds=30)
        draft = planner.build(brief, plan_id="p", spendable_rub=5000.0)
        step = next(s for s in draft.steps if s.format.value == "video")
        spec = caps.get("video", step.model)
        bounds = spec.duration_bounds()
        if bounds and "duration" in step.params:
            assert bounds[0] <= step.params["duration"] <= bounds[1]

    def test_prompt_respects_prompt_max(self, planner, caps):
        brief = make_brief(
            formats=["image"],
            budget_rub=5000.0,
            product_description="описание " * 300,
        )
        draft = planner.build(brief, plan_id="p", spendable_rub=5000.0)
        for step in draft.steps:
            spec = caps.get(step.type, step.model) if step.model else None
            if spec and spec.prompt_max:
                assert len(step.params["prompt"]) <= spec.prompt_max


class TestDeterminismAndExplainability:
    def test_same_brief_yields_same_plan(self, planner):
        brief = make_brief(formats=["image", "voice", "video"], budget_rub=800.0)
        first = planner.build(brief, plan_id="p", spendable_rub=800.0)
        second = planner.build(brief, plan_id="p", spendable_rub=800.0)
        assert [(s.step_id, s.model, s.params) for s in first.steps] == [
            (s.step_id, s.model, s.params) for s in second.steps
        ]

    def test_every_step_explains_itself(self, planner):
        draft = planner.build(
            make_brief(formats=["image", "voice"], budget_rub=800.0),
            plan_id="p",
            spendable_rub=800.0,
        )
        for step in draft.steps:
            assert step.reason
            assert step.cost_basis
            if step.model:
                assert step.model in step.reason
                assert step.rejected_alternatives


class TestStrategies:
    def test_cheapest_strategy_picks_the_cheapest_compatible_model(self, planner, caps):
        draft = planner.build(
            make_brief(formats=["image"], budget_rub=800.0, strategy=SelectionStrategy.CHEAPEST),
            plan_id="p",
            spendable_rub=800.0,
        )
        step = draft.steps[0]
        cheapest = min(

                price_bounds(spec).upper
                for spec in caps.models_for("image")
                if not Policy.load(None).is_excluded(spec.key) and spec.price is not None

        )
        assert step.estimated_cost_rub == pytest.approx(cheapest)

    def test_balanced_upgrades_within_budget(self, planner):
        cheap = planner.build(
            make_brief(formats=["image"], budget_rub=800.0, strategy=SelectionStrategy.CHEAPEST),
            plan_id="p",
            spendable_rub=800.0,
        )
        balanced = planner.build(
            make_brief(formats=["image"], budget_rub=800.0, strategy=SelectionStrategy.BALANCED),
            plan_id="p",
            spendable_rub=800.0,
        )
        assert balanced.steps[0].estimated_cost_rub >= cheap.steps[0].estimated_cost_rub

    def test_upgrades_never_break_the_budget(self, planner):
        for budget in (10, 25, 60, 150, 400, 1200):
            draft = planner.build(
                make_brief(formats=["image", "voice", "video", "music"], budget_rub=float(budget)),
                plan_id="p",
                spendable_rub=float(budget),
            )
            assert draft.total_cost <= budget, f"бюджет {budget}₽ превышен"


class TestPriceBounds:
    def test_tiered_video_uses_conservative_upper_bound(self):
        spec = ModelSpec.parse(
            "grok-ttv",
            "video",
            {"price": 36, "tier_prices": {"grok-ttv-10": 196, "grok-ttv-20": 316},
             "required": ["prompt"]},
        )
        bounds = price_bounds(spec)
        assert bounds.indicative == 36
        assert bounds.upper == 316, "верхняя оценка должна учитывать самый дорогой тир"

    def test_per_second_model_prices_by_duration(self):
        spec = ModelSpec.parse(
            "motion-control-720p",
            "video",
            {"price": None, "per_second": 15, "price_formula": "ceil(...)*15",
             "limits": {"video_duration_max": 30}, "required": ["prompt"]},
        )
        bounds = price_bounds(spec, params={"duration": 8})
        assert bounds.upper == 120

    def test_voice_is_billed_per_started_1000_chars(self):
        spec = ModelSpec.parse("gemini-flash-tts", "voice", {"price": 13, "required": ["prompt"]})
        bounds = price_bounds(spec, params={"prompt": "x" * 2400})
        assert bounds.upper == 39  # 3 начатых тысячи символов

    def test_unknown_price_is_not_selectable(self):
        spec = ModelSpec.parse("mystery", "image", {"required": ["prompt"]})
        bounds = price_bounds(spec)
        assert not bounds.known
        assert bounds.upper == float("inf")


class TestCatalogDrivenBehaviour:
    def test_new_unknown_model_is_still_selectable(self):
        payload = load_capabilities_fixture()
        payload["models"]["image"] = {
            "brand-new-model-x": {"price": 2.0, "description": "new", "required": ["prompt"]}
        }
        planner = Planner(Capabilities.parse(payload), Policy.load(None))
        draft = planner.build(
            make_brief(formats=["image"], budget_rub=100.0), plan_id="p", spendable_rub=100.0
        )
        assert draft.steps[0].model == "brand-new-model-x"

    def test_missing_type_degrades_to_a_free_local_step(self):
        payload = load_capabilities_fixture()
        payload["models"].pop("image")
        planner = Planner(Capabilities.parse(payload), Policy.load(None))
        draft = planner.build(
            make_brief(formats=["image"], budget_rub=100.0), plan_id="p", spendable_rub=100.0
        )
        assert draft.steps[0].estimated_cost_rub == 0.0
        assert any("capabilities" in w for w in draft.warnings)
