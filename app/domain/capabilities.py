"""Typed view over ``GET /capabilities``.

The response is the single source of truth for models, required/optional
parameters, enums and limits. This module parses it defensively: unknown extra
keys are preserved, missing keys never raise, and a model whose shape we do not
understand simply becomes ineligible instead of crashing the planner.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Any

_DURATION_RANGE_RE = re.compile(r"(\d+(?:\.\d+)?)\s*-\s*(\d+(?:\.\d+)?)")


@dataclass(frozen=True)
class ModelSpec:
    """One entry of ``capabilities.models[<type>][<key>]``."""

    key: str
    type: str
    price: float | None
    description: str = ""
    required: tuple[str, ...] = ()
    optional: tuple[str, ...] = ()
    enums: dict[str, list[Any]] = field(default_factory=dict)
    limits: dict[str, Any] = field(default_factory=dict)
    tier_prices: dict[str, float] = field(default_factory=dict)
    per_second: float | None = None
    price_formula: str | None = None
    preserves_input: bool | None = None
    voices: tuple[str, ...] = ()
    note: str = ""
    hint: str = ""
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def known_params(self) -> set[str]:
        return set(self.required) | set(self.optional)

    @property
    def is_token_billed(self) -> bool:
        """Text models are billed per actual tokens: the catalog cannot price them.

        ``/generate/estimate`` still bounds the cost — it reports the reserve the
        platform holds for ``max_tokens`` (``balance.current - balance.after_reserve``).
        """
        return (
            self.price is None
            and self.per_second is None
            and not self.tier_prices
            and "max_tokens" in self.known_params
        )

    @property
    def prompt_max(self) -> int | None:
        value = self.limits.get("prompt_max")
        return int(value) if isinstance(value, (int, float)) else None

    def missing_required(self, available: set[str]) -> list[str]:
        return [p for p in self.required if p not in available]

    def allowed_values(self, param: str) -> list[Any] | None:
        values = self.enums.get(param)
        return list(values) if isinstance(values, list) and values else None

    def duration_bounds(self) -> tuple[float, float] | None:
        """Parse duration limits expressed either as ``"1-15s"`` or ``{max: 30}``."""
        allowed = self.allowed_values("duration")
        if allowed:
            numeric = [float(v) for v in allowed if isinstance(v, (int, float))]
            if numeric:
                return min(numeric), max(numeric)
        raw = self.limits.get("duration")
        if isinstance(raw, str):
            match = _DURATION_RANGE_RE.search(raw)
            if match:
                return float(match.group(1)), float(match.group(2))
        if isinstance(raw, (int, float)):
            return 1.0, float(raw)
        for lo_key, hi_key in (("video_duration_min", "video_duration_max"),):
            hi = self.limits.get(hi_key)
            if isinstance(hi, (int, float)):
                lo = self.limits.get(lo_key)
                return (float(lo) if isinstance(lo, (int, float)) else 1.0), float(hi)
        return None

    @classmethod
    def parse(cls, key: str, type_: str, payload: dict[str, Any]) -> ModelSpec:
        def _seq(name: str) -> tuple[str, ...]:
            value = payload.get(name)
            return tuple(str(v) for v in value) if isinstance(value, list) else ()

        raw_price = payload.get("price")
        price = float(raw_price) if isinstance(raw_price, (int, float)) else None
        tiers_raw = payload.get("tier_prices")
        tier_prices = (
            {str(k): float(v) for k, v in tiers_raw.items() if isinstance(v, (int, float))}
            if isinstance(tiers_raw, dict)
            else {}
        )
        per_second = payload.get("per_second")
        return cls(
            key=key,
            type=type_,
            price=price,
            description=str(payload.get("description") or ""),
            required=_seq("required"),
            optional=_seq("optional"),
            enums=payload["enums"] if isinstance(payload.get("enums"), dict) else {},
            limits=payload["limits"] if isinstance(payload.get("limits"), dict) else {},
            tier_prices=tier_prices,
            per_second=float(per_second) if isinstance(per_second, (int, float)) else None,
            price_formula=payload.get("price_formula"),
            preserves_input=payload.get("preserves_input"),
            voices=_seq("voices"),
            note=str(payload.get("note") or ""),
            hint=str(payload.get("hint") or ""),
            raw=payload,
        )


@dataclass(frozen=True)
class Capabilities:
    """Parsed ``GET /capabilities`` payload."""

    types: tuple[str, ...]
    models_by_type: dict[str, dict[str, ModelSpec]]
    endpoints: dict[str, str] = field(default_factory=dict)
    features: dict[str, Any] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def parse(cls, payload: dict[str, Any]) -> Capabilities:
        models_raw = payload.get("models")
        models: dict[str, dict[str, ModelSpec]] = {}
        if isinstance(models_raw, dict):
            for type_, entries in models_raw.items():
                if not isinstance(entries, dict):
                    continue
                models[str(type_)] = {
                    str(key): ModelSpec.parse(str(key), str(type_), spec)
                    for key, spec in entries.items()
                    if isinstance(spec, dict)
                }
        types_raw = payload.get("types")
        types = tuple(str(t) for t in types_raw) if isinstance(types_raw, list) else tuple(models)
        return cls(
            types=types,
            models_by_type=models,
            endpoints=payload["endpoints"] if isinstance(payload.get("endpoints"), dict) else {},
            features=payload["features"] if isinstance(payload.get("features"), dict) else {},
            raw=payload,
        )

    def models_for(self, type_: str) -> list[ModelSpec]:
        """Models of a type, in a deterministic (key-sorted) order."""
        entries = self.models_by_type.get(type_, {})
        return [entries[key] for key in sorted(entries)]

    def get(self, type_: str, key: str) -> ModelSpec | None:
        return self.models_by_type.get(type_, {}).get(key)

    def has_models_for(self, type_: str) -> bool:
        return bool(self.models_by_type.get(type_))


@dataclass(frozen=True)
class PriceBounds:
    """Indicative and conservative-upper price derived from ``/capabilities``.

    ``upper`` is what the budget guard uses before an authoritative
    ``/generate/estimate`` reply is available: when the catalog is ambiguous
    (tiered video prices, per-second formulas, per-1000-character voice billing)
    we always round *up*, so pre-flight planning can only over-estimate cost.
    """

    indicative: float
    upper: float
    basis: str
    known: bool = True


def price_bounds(
    spec: ModelSpec,
    *,
    params: dict[str, Any] | None = None,
    per_1000_chars_types: frozenset[str] = frozenset({"voice"}),
) -> PriceBounds:
    """Derive price bounds for a model at the parameters we intend to send."""
    params = params or {}

    # 1. Per-second formulas (e.g. motion-control): price scales with duration.
    if spec.per_second:
        duration = params.get("video_duration") or params.get("duration")
        bounds = spec.duration_bounds() or (3.0, 30.0)
        seconds = float(duration) if isinstance(duration, (int, float)) else bounds[1]
        seconds = max(bounds[0], min(seconds, bounds[1]))
        cost = math.ceil(seconds) * spec.per_second
        worst = math.ceil(bounds[1]) * spec.per_second
        return PriceBounds(
            indicative=cost,
            upper=max(cost, worst if duration is None else cost),
            basis=(
                f"per_second={spec.per_second}₽ × {math.ceil(seconds)}s "
                f"({spec.price_formula})"
            ),
        )

    # 2. Tiered models: the catalog's `price` is the default-parameter price while
    #    `tier_prices` shows what longer variants cost. Budget against the worst case.
    if spec.tier_prices:
        worst = max(spec.tier_prices.values())
        base = spec.price if spec.price is not None else worst
        return PriceBounds(
            indicative=base,
            upper=max(base, worst),
            basis=(
                f"price={base}₽ при дефолтных параметрах, консервативная верхняя оценка "
                f"по tier_prices={worst}₽"
            ),
        )

    if spec.price is None:
        basis = (
            "оплата по фактическим токенам — потолок стоимости даёт только "
            "/generate/estimate (резерв под max_tokens)"
            if spec.is_token_billed
            else "цена не опубликована в /capabilities — только через /generate/estimate"
        )
        return PriceBounds(
            indicative=float("inf"), upper=float("inf"), basis=basis, known=False
        )

    # 3. Character-billed types (voice): the catalog price is per started 1000 chars.
    if spec.type in per_1000_chars_types:
        prompt = str(params.get("prompt") or "")
        chunks = max(1, math.ceil(len(prompt) / 1000)) if prompt else 1
        cost = spec.price * chunks
        return PriceBounds(
            indicative=cost,
            upper=cost,
            basis=(
                f"{spec.price}₽ за начатую 1000 символов × {chunks} "
                f"(длина промпта {len(prompt)})"
            ),
        )

    # 4. Flat per-request price.
    return PriceBounds(
        indicative=spec.price,
        upper=spec.price,
        basis=f"фиксированная цена {spec.price}₽ за запрос",
    )
