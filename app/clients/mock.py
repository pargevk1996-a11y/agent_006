"""In-memory mock of the Agent API.

Backed by a real snapshot of ``GET /capabilities`` (``fixtures/capabilities.json``,
captured from the live public endpoint) so the mock demo exercises the same model
catalog, parameter names and price shapes as production — without a token, a
network call or a single ruble spent.

The mock is deliberately strict: it enforces ``required`` parameters, rejects
unknown parameters when ``strict=true``, refuses to charge past the simulated
balance/daily limit, and replays an identical response for a repeated
``idempotency_key`` instead of charging twice.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from app.clients.base import VibeClient
from app.clients.exceptions import VibeAPIError
from app.domain.capabilities import Capabilities, price_bounds

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "capabilities.json"

#: Prompts containing this marker make the mock fail that generation (with refund),
#: which is how the partial-failure path is demonstrated and tested.
FAILURE_MARKER = "__mock_fail__"

#: Approximate ₽ per token for the reserve of token-billed text models, calibrated on
#: live /generate/estimate responses. Only the mock uses these — real pricing always
#: comes from the platform.
TOKEN_RATE_RUB = {"claude": 0.0076, "gpt": 0.0091}
DEFAULT_TOKEN_RATE_RUB = 0.008

#: Share of the reserve the mock actually charges, mirroring "billed per real tokens".
TOKEN_ACTUAL_SHARE = 0.6


def load_capabilities_fixture() -> dict[str, Any]:
    return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


class MockVibeClient(VibeClient):
    """Deterministic local stand-in for :class:`~app.clients.vibe.HttpVibeClient`."""

    mode = "mock"

    def __init__(
        self,
        *,
        balance_rub: float = 5_000.0,
        daily_limit_rub: float = 5_000.0,
        daily_spent_rub: float = 0.0,
        capabilities_payload: dict[str, Any] | None = None,
        price_multiplier: float = 1.0,
        complete_after_polls: int = 0,
        fail_models: set[str] | None = None,
    ) -> None:
        self._payload = capabilities_payload or load_capabilities_fixture()
        self._caps = Capabilities.parse(self._payload)
        self.balance_rub = balance_rub
        self.daily_limit_rub = daily_limit_rub
        self.daily_spent_rub = daily_spent_rub
        self.price_multiplier = price_multiplier
        self.complete_after_polls = complete_after_polls
        self.fail_models = fail_models or set()

        self.calls: list[tuple[str, dict[str, Any]]] = []
        self._generations: dict[int, dict[str, Any]] = {}
        self._by_idempotency: dict[str, dict[str, Any]] = {}
        self._poll_counts: dict[int, int] = {}
        self._next_id = 5811

    # -- helpers -----------------------------------------------------------
    def _record(self, name: str, payload: dict[str, Any]) -> None:
        self.calls.append((name, payload))

    def calls_of(self, name: str) -> list[dict[str, Any]]:
        return [payload for called, payload in self.calls if called == name]

    def _spec_or_error(self, body: dict[str, Any]):
        type_ = str(body.get("type") or "")
        model_key = str(body.get("model") or "")
        spec = self._caps.get(type_, model_key)
        if spec is None:
            raise VibeAPIError(
                422,
                code="model_not_supported",
                message=f"Модель {model_key!r} недоступна для type={type_!r}.",
                request_id="mock-" + hashlib.sha1(model_key.encode()).hexdigest()[:12],  # noqa: S324
            )
        return spec

    def _validate(self, body: dict[str, Any], spec) -> tuple[list[str], list[str]]:
        supplied = {k for k in body if k not in {"type", "model", "strict", "idempotency_key"}}
        missing = [p for p in spec.required if p not in body or body.get(p) in (None, "", [])]
        allowed = spec.known_params | {"callback_url", "generation_type"}
        rejected = sorted(supplied - allowed)
        if missing:
            raise VibeAPIError(
                422,
                code="validation_failed",
                message="Не заполнены обязательные поля.",
                details={p: ["Поле обязательно."] for p in missing},
                request_id="mock-validation",
            )
        if rejected and body.get("strict") is True:
            raise VibeAPIError(
                422,
                code="validation_failed",
                message="strict=true: переданы неизвестные параметры.",
                details={p: ["Параметр не поддерживается моделью."] for p in rejected},
                request_id="mock-strict",
            )
        return missing, rejected

    def _price(self, body: dict[str, Any], spec) -> tuple[float, bool]:
        """Return ``(cost, is_reserve)``.

        Token-billed text models have no catalog price: the platform answers with a
        *reserve* for ``max_tokens`` instead of a fixed cost, and bills real tokens
        afterwards. The mock reproduces that shape (the per-token rate here is an
        approximation of the observed live behaviour, not a published price).
        """
        if spec.is_token_billed:
            max_tokens = body.get("max_tokens")
            tokens = float(max_tokens) if isinstance(max_tokens, (int, float)) else 1000.0
            rate = TOKEN_RATE_RUB.get(spec.key.split("-")[0], DEFAULT_TOKEN_RATE_RUB)
            return round(tokens * rate * self.price_multiplier, 2), True

        bounds = price_bounds(spec, params=body)
        if not bounds.known:
            raise VibeAPIError(
                422,
                code="validation_failed",
                message=f"Для модели {spec.key} цена вычисляется только по фактическим параметрам.",
                request_id="mock-price",
            )
        return round(bounds.indicative * self.price_multiplier, 2), False

    # -- endpoints ---------------------------------------------------------
    async def capabilities(self) -> dict[str, Any]:
        self._record("capabilities", {})
        return self._payload

    async def me(self) -> dict[str, Any]:
        self._record("me", {})
        return {
            "status": "ok",
            "token": {"id": 1, "name": "mock-token", "scopes": ["read", "generate"]},
            "balance": self.balance_rub,
            "daily_spend_limit": self.daily_limit_rub,
            "daily_spent": self.daily_spent_rub,
        }

    async def balance(self) -> dict[str, Any]:
        self._record("balance", {})
        return {"status": "ok", "balance": self.balance_rub, "currency": "RUB"}

    async def estimate(self, body: dict[str, Any]) -> dict[str, Any]:
        self._record("estimate", body)
        spec = self._spec_or_error(body)
        _, rejected = self._validate(body, spec)
        cost, is_reserve = self._price(body, spec)
        within_daily = (self.daily_spent_rub + cost) <= self.daily_limit_rub
        balance_block = (
            {"current": self.balance_rub, "after_reserve": round(self.balance_rub - cost, 2)}
            if is_reserve
            else {"current": self.balance_rub, "after": round(self.balance_rub - cost, 2)}
        )
        return {
            "valid": True,
            "dry_run": True,
            "model": spec.key,
            "type": spec.type,
            # Token-billed models report no fixed cost — only the reserve, as live does.
            "estimated_cost_rub": None if is_reserve else cost,
            "balance": balance_block,
            "daily_spend": {
                "limit": self.daily_limit_rub,
                "today": self.daily_spent_rub,
                "within_limit": within_daily,
            },
            "validation": {"required_missing": []},
            "rejected": rejected,
            "valid_params": sorted(spec.known_params),
            "warnings": [],
        }

    async def generate(self, body: dict[str, Any]) -> dict[str, Any]:
        self._record("generate", body)
        key = body.get("idempotency_key")
        if not key:
            raise VibeAPIError(422, code="validation_failed", message="idempotency_key required")
        if key in self._by_idempotency:
            # Replay: identical response, no second charge.
            return self._by_idempotency[key]

        spec = self._spec_or_error(body)
        self._validate(body, spec)
        reserve, is_reserve = self._price(body, spec)
        # Token-billed models are charged for the tokens actually produced, which is
        # at most the reserve. Everything else is charged its fixed price.
        cost = round(reserve * TOKEN_ACTUAL_SHARE, 2) if is_reserve else reserve

        if reserve > self.balance_rub:
            raise VibeAPIError(
                402,
                code="insufficient_balance",
                message=f"Недостаточно средств: нужно {reserve}₽, доступно {self.balance_rub}₽.",
                request_id="mock-balance",
            )
        if self.daily_spent_rub + reserve > self.daily_limit_rub:
            raise VibeAPIError(
                429,
                code="daily_spend_limit_exceeded",
                message="Достигнут дневной лимит трат токена.",
                request_id="mock-daily",
                retry_after=3600,
            )

        self.balance_rub = round(self.balance_rub - cost, 2)
        self.daily_spent_rub = round(self.daily_spent_rub + cost, 2)
        generation_id = self._next_id
        self._next_id += 1

        will_fail = spec.key in self.fail_models or FAILURE_MARKER in str(body.get("prompt", ""))
        now = datetime.now(UTC)
        self._generations[generation_id] = {
            "generation_id": generation_id,
            "task_id": f"task_mock_{generation_id}",
            "model": spec.key,
            "type": spec.type,
            "cost": cost,
            "will_fail": will_fail,
            "created_at": now.isoformat(),
            "idempotency_key": key,
            "text": (
                f"[mock {spec.key}] Рекламный текст по брифу: "
                f"{str(body.get('prompt', ''))[:120]}…"
                if spec.type == "text"
                else None
            ),
        }
        response = {
            "status": "processing",
            "generation_id": generation_id,
            "task_id": f"task_mock_{generation_id}",
            "cost": cost,
            "balance_after": self.balance_rub,
        }
        self._by_idempotency[key] = response
        return response

    async def generation_status(self, generation_id: int | str) -> dict[str, Any]:
        self._record("generation_status", {"generation_id": generation_id})
        gid = int(generation_id)
        record = self._generations.get(gid)
        if record is None:
            raise VibeAPIError(404, code="not_found", message="Генерация не найдена.")

        polls = self._poll_counts.get(gid, 0)
        self._poll_counts[gid] = polls + 1
        created = datetime.fromisoformat(record["created_at"])
        base = {
            "generation_id": gid,
            "task_id": record["task_id"],
            "model": record["model"],
            "type": record["type"],
            "created_at": record["created_at"],
            "updated_at": (created + timedelta(seconds=42)).isoformat(),
        }
        if polls < self.complete_after_polls:
            return {**base, "status": "processing", "cost": record["cost"]}

        if record["will_fail"]:
            # The platform refunds automatically when a provider fails after start.
            if not record.get("refunded"):
                record["refunded"] = True
                self.balance_rub = round(self.balance_rub + record["cost"], 2)
                self.daily_spent_rub = round(self.daily_spent_rub - record["cost"], 2)
            return {
                **base,
                "status": "error",
                "cost": 0.0,
                "error_message": "mock: провайдер вернул ошибку, средства возвращены",
                "refunded": True,
            }
        if record.get("text"):
            return {
                **base,
                "status": "complete",
                "cost": record["cost"],
                "text": record["text"],
                "display_url": f"https://lk.vibemarketolog.ru/files/generation/{gid}?mock=1",
                "refunded": False,
            }
        return {
            **base,
            "status": "complete",
            "cost": record["cost"],
            "result_url": f"https://mock.local/result/{gid}.bin",
            "display_url": f"https://lk.vibemarketolog.ru/files/generation/{gid}?mock=1",
            "refunded": False,
        }

    async def aclose(self) -> None:
        return None
