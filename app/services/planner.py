"""Model selection and plan construction.

Selection is deterministic and explainable:

1. **Compatibility** — a model is eligible only if every parameter in its
   ``required`` list (from ``/capabilities``) is something this agent can supply
   for this brief. Nothing is hardcoded about *which* parameters those are.
2. **Parameters** — the request body is built from the model's own
   ``required``/``optional``/``enums``/``limits``, so ``strict: true`` cannot be
   tripped by an unsupported field.
3. **Price** — taken from ``/capabilities`` and always rounded *up* when the
   catalog is ambiguous (tiers, per-second formulas, per-1000-character billing).
   This is a pre-filter only; ``/generate/estimate`` is authoritative.
4. **Ranking** — a small configurable policy table gives quality tiers; ties break
   on price, then model key. Models unknown to the table are still selectable,
   they just sit in the neutral fallback tier.
5. **Budget fitting** — start from the cheapest viable set, drop lowest-priority
   formats until the set fits, then upgrade greedily while it still fits.
"""

from __future__ import annotations

import hashlib
import logging
import math
import uuid
from dataclasses import dataclass, field
from typing import Any

from app.domain.brief import Brief, ContentFormat, SelectionStrategy
from app.domain.capabilities import Capabilities, ModelSpec, PriceBounds, price_bounds
from app.domain.plan import PlanStep, RejectedCandidate, StepKind
from app.domain.policy import FALLBACK_TIER, Policy
from app.services import prompts

logger = logging.getLogger(__name__)

MEDIA_PARAMS_FROM_IMAGES = (
    "image_urls",
    "image_input",
    "first_frame_url",
    "reference_image_urls",
)
MAX_REPORTED_ALTERNATIVES = 6
MAX_UPGRADE_ROUNDS = 20

#: Namespace for deterministic idempotency keys (uuid5).
IDEMPOTENCY_NAMESPACE = uuid.UUID("6f9f3f5c-2a1b-4f2e-9f0d-8c1d2e3f4a5b")


@dataclass
class Candidate:
    spec: ModelSpec
    tier: int
    bounds: PriceBounds
    params: dict[str, Any]
    param_warnings: list[str] = field(default_factory=list)

    @property
    def key(self) -> str:
        return self.spec.key

    @property
    def cost(self) -> float:
        return self.bounds.upper


@dataclass
class FormatAnalysis:
    fmt: ContentFormat
    type_: str
    candidates: list[Candidate]
    rejected: list[RejectedCandidate]


@dataclass
class PlanDraft:
    steps: list[PlanStep]
    warnings: list[str]
    dropped_formats: list[ContentFormat]

    @property
    def total_cost(self) -> float:
        return round(sum(s.estimated_cost_rub for s in self.steps), 4)


class Planner:
    def __init__(self, capabilities: Capabilities, policy: Policy) -> None:
        self.caps = capabilities
        self.policy = policy

    # -- public API --------------------------------------------------------
    def build(self, brief: Brief, *, plan_id: str, spendable_rub: float) -> PlanDraft:
        warnings: list[str] = []
        analyses: dict[ContentFormat, FormatAnalysis] = {}
        local_steps: list[PlanStep] = []
        ordered = sorted(brief.formats, key=lambda f: self.policy.priority_of(f.value))

        for fmt in ordered:
            if not self.caps.has_models_for(fmt.value):
                local_steps.append(self._local_step(brief, fmt, plan_id=plan_id))
                warnings.append(
                    f"Формат «{fmt.value}»: в GET /capabilities нет моделей этого типа "
                    f"(доступны: {', '.join(sorted(self.caps.models_by_type))}). "
                    "Шаг выполнен локально и бесплатно, платная генерация не запускается."
                )
                continue
            analysis = self._analyse(brief, fmt)
            if not analysis.candidates:
                warnings.append(
                    f"Формат «{fmt.value}» пропущен: ни одна модель из /capabilities не совместима "
                    f"с брифом ({len(analysis.rejected)} кандидатов отклонено)."
                )
                continue
            analyses[fmt] = analysis

        selection, dropped, fit_warnings = self._fit_to_budget(analyses, brief, spendable_rub)
        warnings.extend(fit_warnings)

        steps: list[PlanStep] = list(local_steps)
        for fmt, candidate in selection.items():
            steps.append(
                self._to_step(
                    brief,
                    fmt,
                    candidate,
                    analyses[fmt],
                    plan_id=plan_id,
                    selection={f: c.key for f, c in selection.items()},
                )
            )
        steps.sort(key=lambda s: self.policy.priority_of(s.format.value))
        return PlanDraft(steps=steps, warnings=warnings, dropped_formats=dropped)

    # -- candidate analysis ------------------------------------------------
    def _analyse(self, brief: Brief, fmt: ContentFormat) -> FormatAnalysis:
        available = self._available_params(brief)
        candidates: list[Candidate] = []
        rejected: list[RejectedCandidate] = []

        for spec in self.caps.models_for(fmt.value):
            if self.policy.is_excluded(spec.key):
                rejected.append(
                    RejectedCandidate(
                        model=spec.key,
                        reason="исключена policy-таблицей (служебная/цепочечная модель)",
                    )
                )
                continue
            missing = spec.missing_required(available)
            if missing:
                rejected.append(
                    RejectedCandidate(
                        model=spec.key,
                        reason=(
                            "нет обязательных параметров: "
                            + ", ".join(missing)
                            + " — бриф не содержит нужных исходников"
                        ),
                    )
                )
                continue
            params, param_warnings = self._build_params(brief, fmt, spec)
            bounds = price_bounds(
                spec, params=params, per_1000_chars_types=self.policy.per_1000_chars_types
            )
            if not bounds.known:
                rejected.append(
                    RejectedCandidate(model=spec.key, reason=bounds.basis)
                )
                continue
            candidates.append(
                Candidate(
                    spec=spec,
                    tier=self.policy.tier_of(fmt.value, spec.key),
                    bounds=bounds,
                    params=params,
                    param_warnings=param_warnings,
                )
            )

        candidates.sort(key=lambda c: self._preference_key(c, brief.strategy))
        return FormatAnalysis(fmt=fmt, type_=fmt.value, candidates=candidates, rejected=rejected)

    def _preference_key(self, candidate: Candidate, strategy: SelectionStrategy) -> tuple:
        if strategy is SelectionStrategy.QUALITY:
            # Within a tier prefer the richer (more expensive) option.
            return (candidate.tier, -candidate.cost, candidate.key)
        return (candidate.tier, candidate.cost, candidate.key)

    def _available_params(self, brief: Brief) -> set[str]:
        available = set(self.policy.suppliable_params)
        if brief.reference_image_urls:
            available.update(MEDIA_PARAMS_FROM_IMAGES)
        if brief.landing_url:
            available.add("source_url")
        return available

    # -- parameter construction -------------------------------------------
    def _build_params(
        self, brief: Brief, fmt: ContentFormat, spec: ModelSpec
    ) -> tuple[dict[str, Any], list[str]]:
        """Build a request body honouring the model's own enums and limits."""
        warnings: list[str] = []
        desired = self._desired_params(brief, fmt)
        params: dict[str, Any] = {}

        for name in sorted(spec.known_params):
            if name in {"prompt", "callback_url", "strict", "idempotency_key"}:
                continue
            if name not in desired or desired[name] is None:
                continue
            value, note = self._coerce(spec, name, desired[name])
            if value is None:
                if note:
                    warnings.append(note)
                continue
            params[name] = value
            if note:
                warnings.append(note)

        prompt = prompts.prompt_for(brief, fmt)
        limit = spec.prompt_max
        if limit and len(prompt) > limit:
            prompt = prompt[: limit - 1].rstrip() + "…"
            warnings.append(
                f"{spec.key}: промпт усечён до {limit} символов (limits.prompt_max)."
            )
        params["prompt"] = prompt

        # image-to-video needs an explicit switch on the Veo family.
        if (
            "generation_type" in spec.known_params
            and params.get("image_urls")
            and "generation_type" not in params
        ):
            params["generation_type"] = "image-to-video"
        return params, warnings

    def _desired_params(self, brief: Brief, fmt: ContentFormat) -> dict[str, Any]:
        defaults = self.policy.defaults
        desired: dict[str, Any] = {
            "aspect_ratio": brief.aspect_ratio or defaults.get("aspect_ratio"),
            "language_code": brief.language or defaults.get("language_code"),
            "lang": brief.language or defaults.get("language_code"),
        }
        if fmt is ContentFormat.VIDEO:
            desired["duration"] = brief.video_duration_seconds or defaults.get(
                "video_duration_seconds"
            )
            desired["resolution"] = defaults.get("resolution")
            if brief.reference_image_urls:
                desired["image_urls"] = list(brief.reference_image_urls)
                desired["first_frame_url"] = brief.reference_image_urls[0]
                desired["reference_image_urls"] = list(brief.reference_image_urls)
            if brief.landing_url:
                desired["source_url"] = brief.landing_url
        elif fmt is ContentFormat.IMAGE:
            if brief.reference_image_urls:
                desired["image_input"] = brief.reference_image_urls[0]
                desired["image_urls"] = list(brief.reference_image_urls)
        elif fmt is ContentFormat.VOICE:
            desired["voice_id"] = defaults.get("voice_id")
            desired["voice_name"] = defaults.get("voice_name")
        elif fmt is ContentFormat.MUSIC:
            desired["music_style"] = brief.style or None
            desired["style_tags"] = brief.style or None
            desired["vocal_gender"] = defaults.get("vocal_gender")
        return desired

    def _coerce(self, spec: ModelSpec, name: str, value: Any) -> tuple[Any, str | None]:
        """Fit a desired value into what the model actually accepts."""
        allowed = spec.allowed_values(name)

        if name == "duration":
            bounds = spec.duration_bounds()
            numeric = float(value)
            if allowed:
                options = sorted(float(v) for v in allowed if isinstance(v, (int, float)))
                if options:
                    fitting = [o for o in options if o <= numeric] or [options[0]]
                    chosen = max(fitting)
                    note = (
                        None
                        if chosen == numeric
                        else f"{spec.key}: duration {numeric:g}s → {chosen:g}s (enums.duration)"
                    )
                    return (int(chosen) if chosen.is_integer() else chosen), note
            if bounds:
                clamped = max(bounds[0], min(numeric, bounds[1]))
                note = (
                    None
                    if clamped == numeric
                    else f"{spec.key}: duration {numeric:g}s → {clamped:g}s (limits.duration)"
                )
                return (int(clamped) if float(clamped).is_integer() else clamped), note
            return (int(numeric) if float(numeric).is_integer() else numeric), None

        if name == "voice_name" and spec.voices:
            if value in spec.voices:
                return value, None
            return spec.voices[0], f"{spec.key}: голос {value!r} недоступен → {spec.voices[0]!r}"

        if isinstance(value, list):
            limit = spec.limits.get(name)
            if isinstance(limit, int) and len(value) > limit:
                return value[:limit], f"{spec.key}: {name} усечён до {limit} элементов."
            return value, None

        if allowed:
            if value in allowed:
                return value, None
            fallback = allowed[0]
            return (
                fallback,
                f"{spec.key}: {name}={value!r} не поддерживается "
                f"(enums={allowed}) → {fallback!r}",
            )

        if isinstance(value, str):
            limit = spec.limits.get(f"{name}_max")
            if isinstance(limit, int) and len(value) > limit:
                return value[:limit], f"{spec.key}: {name} усечён до {limit} символов."
        return value, None

    # -- budget fitting ----------------------------------------------------
    def _fit_to_budget(
        self,
        analyses: dict[ContentFormat, FormatAnalysis],
        brief: Brief,
        spendable_rub: float,
    ) -> tuple[dict[ContentFormat, Candidate], list[ContentFormat], list[str]]:
        warnings: list[str] = []
        dropped: list[ContentFormat] = []
        ordered = sorted(analyses, key=lambda f: self.policy.priority_of(f.value))

        # 1. Baseline: the cheapest compatible model for every format.
        selection: dict[ContentFormat, Candidate] = {}
        for fmt in ordered:
            cheapest = min(
                analyses[fmt].candidates, key=lambda c: (c.cost, c.tier, c.key)
            )
            selection[fmt] = cheapest

        def total() -> float:
            return round(sum(c.cost for c in selection.values()), 4)

        # 2. Drop lowest-priority formats until the baseline fits.
        while selection and total() > spendable_rub:
            victim = max(selection, key=lambda f: (self.policy.priority_of(f.value), f.value))
            cost = selection.pop(victim).cost
            dropped.append(victim)
            warnings.append(
                f"Формат «{victim.value}» исключён из плана: даже самая дешёвая совместимая "
                f"модель стоит {cost:.2f}₽, а бюджет уже исчерпан "
                f"(доступно к распределению {spendable_rub:.2f}₽)."
            )

        if not selection and not dropped:
            return selection, dropped, warnings

        # 3. Greedy upgrade: give each format, in priority order, the most preferred
        #    model that still keeps the whole plan inside the budget.
        if brief.strategy is not SelectionStrategy.CHEAPEST:
            for _ in range(MAX_UPGRADE_ROUNDS):
                changed = False
                for fmt in [f for f in ordered if f in selection]:
                    current = selection[fmt]
                    others = total() - current.cost
                    for candidate in analyses[fmt].candidates:  # already preference-sorted
                        if candidate.key == current.key:
                            break
                        if others + candidate.cost <= spendable_rub:
                            selection[fmt] = candidate
                            changed = True
                            break
                if not changed:
                    break

        return selection, dropped, warnings

    # -- step construction -------------------------------------------------
    def _to_step(
        self,
        brief: Brief,
        fmt: ContentFormat,
        candidate: Candidate,
        analysis: FormatAnalysis,
        *,
        plan_id: str,
        selection: dict[ContentFormat, str],
    ) -> PlanStep:
        step_id = f"{fmt.value}-1"
        rejected = list(analysis.rejected)
        for other in analysis.candidates:
            if other.key == candidate.key:
                continue
            if other.tier < candidate.tier:
                reason = (
                    f"выше по качеству (tier {other.tier}), но {other.cost:.2f}₽ "
                    "не укладывается в остаток бюджета"
                )
            elif other.cost < candidate.cost:
                reason = (
                    f"дешевле ({other.cost:.2f}₽), но ниже по policy-приоритету "
                    f"(tier {other.tier})"
                )
            else:
                reason = f"дороже ({other.cost:.2f}₽) при tier {other.tier}"
            rejected.append(RejectedCandidate(model=other.key, reason=reason))

        tier_label = (
            "вне policy-таблицы" if candidate.tier == FALLBACK_TIER else f"tier {candidate.tier}"
        )
        reason = (
            f"Модель {candidate.key} выбрана для формата «{fmt.value}»: "
            f"совместима с брифом (required={list(candidate.spec.required) or ['—']}), "
            f"policy-приоритет — {tier_label}, "
            f"цена по /capabilities: {candidate.bounds.basis}. "
            f"Рассмотрено кандидатов: {len(analysis.candidates)} совместимых, "
            f"{len(analysis.rejected)} отклонено на этапе совместимости."
        )
        return PlanStep(
            step_id=step_id,
            format=fmt,
            kind=StepKind.GENERATION,
            type=fmt.value,
            model=candidate.key,
            model_description=candidate.spec.description,
            params=candidate.params,
            idempotency_key=make_idempotency_key(plan_id, step_id, candidate.key, candidate.params),
            estimated_cost_rub=round(candidate.cost, 4),
            cost_source="capabilities",
            cost_basis=candidate.bounds.basis,
            reason=reason,
            rejected_alternatives=rejected[:MAX_REPORTED_ALTERNATIVES],
            warnings=candidate.param_warnings,
        )

    def _local_step(self, brief: Brief, fmt: ContentFormat, *, plan_id: str) -> PlanStep:
        step_id = f"{fmt.value}-1"
        output = prompts.prompt_for(brief, fmt)
        return PlanStep(
            step_id=step_id,
            format=fmt,
            kind=StepKind.LOCAL,
            type=None,
            model=None,
            model_description="Локальная генерация текста без обращения к платному API",
            params={},
            idempotency_key=make_idempotency_key(plan_id, step_id, "local", {}),
            estimated_cost_rub=0.0,
            cost_source="local",
            cost_basis="локальный шаг — 0₽",
            reason=(
                f"В каталоге /capabilities нет моделей типа «{fmt.value}», поэтому агент не "
                "выдумывает модель и не тратит деньги: текст собирается локально по брифу."
            ),
            local_output=output,
        )


def make_idempotency_key(
    plan_id: str, step_id: str, model: str, params: dict[str, Any]
) -> str:
    """Deterministic per-step key.

    Derived from the plan, the step and the exact payload, so a retry of the same
    step always replays the same key, while a different payload can never reuse it.
    """
    payload = repr(sorted((k, repr(v)) for k, v in params.items()))
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]
    return str(uuid.uuid5(IDEMPOTENCY_NAMESPACE, f"{plan_id}|{step_id}|{model}|{digest}"))


def capabilities_fingerprint(payload: dict[str, Any]) -> str:
    """Short hash of the catalog a plan was built against."""
    import json

    blob = json.dumps(payload.get("models", {}), sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def ceil_rub(value: float) -> float:
    return math.ceil(value * 100) / 100
