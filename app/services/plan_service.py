"""Orchestration: brief → priced plan → (confirmed) execution.

Planning never spends: it reads ``/capabilities``, snapshots the account and
prices every step through ``/generate/estimate`` (a documented dry-run).
Execution re-verifies everything from scratch and stops before the first ruble
if anything drifted.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from app.clients.base import VibeClient
from app.clients.exceptions import VibeAPIError, VibeError
from app.core.config import AppMode, Settings
from app.core.errors import (
    ConfirmationRequiredError,
    ConflictError,
    ModeNotAllowedError,
    NotFoundError,
    ValidationError,
)
from app.domain.brief import Brief
from app.domain.capabilities import Capabilities
from app.domain.plan import (
    AccountSnapshot,
    Job,
    JobStatus,
    JobStep,
    Plan,
    PlanStatus,
    PlanStep,
    StepKind,
    StepStatus,
)
from app.domain.policy import Policy
from app.repositories.jobs import JobRepository
from app.repositories.plans import PlanRepository
from app.services.budget import BudgetGuard
from app.services.executor import Executor
from app.services.planner import Planner, capabilities_fingerprint

logger = logging.getLogger(__name__)

#: Price drift below this (in rubles) is treated as rounding, not a change.
PRICE_TOLERANCE_RUB = 0.01


class PlanService:
    def __init__(
        self,
        *,
        client: VibeClient,
        settings: Settings,
        policy: Policy,
        plan_repo: PlanRepository,
        job_repo: JobRepository,
    ) -> None:
        self.client = client
        self.settings = settings
        self.policy = policy
        self.plans = plan_repo
        self.jobs = job_repo

    # ------------------------------------------------------------------
    # Planning (never bills)
    # ------------------------------------------------------------------
    async def create_plan(self, brief: Brief) -> Plan:
        if brief.budget_rub > self.settings.max_budget_rub:
            raise ValidationError(
                f"Бюджет {brief.budget_rub:.2f}₽ превышает жёсткий потолок сервиса "
                f"{self.settings.max_budget_rub:.2f}₽ (MAX_BUDGET_RUB)."
            )

        plan_id = str(uuid.uuid4())
        caps_payload = await self.client.capabilities()
        capabilities = Capabilities.parse(caps_payload)
        account, account_warnings = await self._account_snapshot()

        guard = BudgetGuard(
            budget_rub=brief.budget_rub,
            safety_margin=self.settings.budget_safety_margin,
            account=account,
        )
        planner = Planner(capabilities, self.policy)
        draft = planner.build(brief, plan_id=plan_id, spendable_rub=guard.spendable_rub)

        warnings = [*account_warnings, *draft.warnings]
        steps, estimate_warnings = await self._price_steps(draft.steps, account)
        warnings.extend(estimate_warnings)

        steps, fit_warnings = self._drop_until_affordable(steps, guard.spendable_rub)
        warnings.extend(fit_warnings)

        total = round(sum(s.estimated_cost_rub for s in steps), 4)
        billable = [s for s in steps if s.kind is StepKind.GENERATION]
        if not billable and not steps:
            status = PlanStatus.INFEASIBLE
        elif draft.dropped_formats or fit_warnings or len(steps) < len(brief.formats):
            status = PlanStatus.DEGRADED
        else:
            status = PlanStatus.READY

        if account.balance_rub is not None and total > account.balance_rub:
            warnings.append(
                f"Плановая стоимость {total:.2f}₽ превышает баланс аккаунта "
                f"{account.balance_rub:.2f}₽ — исполнение будет остановлено до списания."
            )
        daily_remaining = account.daily_remaining_rub
        if daily_remaining is not None and total > daily_remaining:
            warnings.append(
                f"Плановая стоимость {total:.2f}₽ превышает остаток дневного лимита "
                f"{daily_remaining:.2f}₽."
            )
        if self.settings.app_mode is not AppMode.LIVE:
            warnings.append(
                f"Режим APP_MODE={self.settings.app_mode.value}: платная генерация отключена, "
                "план носит справочный характер."
            )

        plan = Plan(
            plan_id=plan_id,
            mode=self.settings.app_mode.value,
            status=status,
            brief=brief,
            budget_rub=brief.budget_rub,
            safety_margin_rub=guard.margin_rub,
            spendable_rub=guard.spendable_rub,
            total_estimated_rub=total,
            steps=steps,
            warnings=warnings,
            account=account,
            capabilities_fingerprint=capabilities_fingerprint(caps_payload),
        )
        await self.plans.save(plan)
        logger.info(
            "plan_created",
            extra={
                "plan_id": plan_id,
                "plan_status": status.value,
                "budget_rub": brief.budget_rub,
                "total_estimated_rub": total,
                "steps": [f"{s.step_id}:{s.model or 'local'}" for s in steps],
            },
        )
        return plan

    async def _price_steps(
        self, steps: list[PlanStep], account: AccountSnapshot
    ) -> tuple[list[PlanStep], list[str]]:
        """Replace catalog estimates with authoritative ``/generate/estimate`` prices."""
        warnings: list[str] = []
        priced: list[PlanStep] = []
        for step in steps:
            if step.kind is StepKind.LOCAL:
                priced.append(step)
                continue
            try:
                payload = await self.client.estimate(step.estimate_body())
            except VibeAPIError as exc:
                warnings.append(
                    f"Шаг «{step.step_id}» ({step.model}) исключён: /generate/estimate вернул "
                    f"{exc.code} — {exc.message}"
                )
                continue
            except VibeError as exc:
                warnings.append(
                    f"Шаг «{step.step_id}» ({step.model}) исключён: не удалось получить оценку "
                    f"({exc})."
                )
                continue

            if payload.get("valid") is False:
                warnings.append(
                    f"Шаг «{step.step_id}» ({step.model}) исключён: платформа считает запрос "
                    f"невалидным ({payload.get('validation')})."
                )
                continue

            cost = payload.get("estimated_cost_rub")
            if not isinstance(cost, (int, float)):
                warnings.append(
                    f"Шаг «{step.step_id}» ({step.model}) исключён: платформа не вернула "
                    "estimated_cost_rub (fail-closed)."
                )
                continue

            catalog_cost = step.estimated_cost_rub
            step.estimated_cost_rub = round(float(cost), 4)
            step.cost_source = "estimate"
            step.cost_basis = (
                f"POST /generate/estimate: {step.estimated_cost_rub:.2f}₽ "
                f"(оценка по /capabilities была {catalog_cost:.2f}₽)"
            )
            for note in payload.get("warnings") or []:
                step.warnings.append(f"платформа: {note}")
            if payload.get("rejected"):
                step.warnings.append(
                    f"платформа отклонила параметры: {payload['rejected']} (strict=true)"
                )
            _merge_account(account, payload)
            priced.append(step)
        return priced, warnings

    def _drop_until_affordable(
        self, steps: list[PlanStep], spendable_rub: float
    ) -> tuple[list[PlanStep], list[str]]:
        """Guarantee the plan fits after authoritative pricing."""
        warnings: list[str] = []
        kept = list(steps)
        while round(sum(s.estimated_cost_rub for s in kept), 4) > spendable_rub:
            billable = [s for s in kept if s.kind is StepKind.GENERATION]
            if not billable:
                break
            victim = max(
                billable,
                key=lambda s: (self.policy.priority_of(s.format.value), s.estimated_cost_rub),
            )
            kept.remove(victim)
            warnings.append(
                f"Шаг «{victim.step_id}» ({victim.model}) удалён из плана: после точной оценки "
                f"через /generate/estimate ({victim.estimated_cost_rub:.2f}₽) план не укладывался "
                f"в доступные {spendable_rub:.2f}₽."
            )
        return kept, warnings

    async def _account_snapshot(self) -> tuple[AccountSnapshot, list[str]]:
        """Best-effort account state. Unknown values stay ``None`` (fail-closed later)."""
        warnings: list[str] = []
        snapshot = AccountSnapshot(source=self.settings.app_mode.value)
        try:
            me = await self.client.me()
            snapshot.balance_rub = _first_number(me, ("balance", "balance_rub"))
            snapshot.daily_limit_rub = _first_number(
                me, ("daily_spend_limit", "daily_limit", "daily_spend_limit_rub")
            )
            snapshot.daily_spent_rub = _first_number(
                me, ("daily_spent", "spent_today", "daily_spent_rub")
            )
        except VibeError as exc:
            warnings.append(f"GET /me недоступен ({exc}) — состояние аккаунта неизвестно.")
        if snapshot.balance_rub is None:
            try:
                balance = await self.client.balance()
                snapshot.balance_rub = _first_number(balance, ("balance", "balance_rub", "amount"))
            except VibeError as exc:
                warnings.append(f"GET /balance недоступен ({exc}).")
        if snapshot.balance_rub is None:
            warnings.append(
                "Баланс аккаунта неизвестен: планирование продолжено, но исполнение будет "
                "заблокировано (fail-closed)."
            )
        return snapshot, warnings

    # ------------------------------------------------------------------
    # Execution (bills, and only with explicit confirmation)
    # ------------------------------------------------------------------
    async def execute_plan(self, plan_id: str, *, confirmed: bool) -> Job:
        plan = await self.plans.get(plan_id)
        if plan is None:
            raise NotFoundError(f"План {plan_id} не найден.")
        if not confirmed:
            raise ConfirmationRequiredError(
                "Исполнение требует явного подтверждения: передайте {\"confirmed\": true}."
            )
        if self.settings.app_mode is AppMode.ESTIMATE:
            raise ModeNotAllowedError(
                "APP_MODE=estimate: разрешены только оценки. Для реальной генерации "
                "перезапустите сервис с APP_MODE=live."
            )
        if plan.status is PlanStatus.INFEASIBLE:
            raise ConflictError("План невыполним: нет ни одного шага, укладывающегося в бюджет.")

        existing = await self.jobs.get_by_plan(plan_id)
        if existing is not None and (
            existing.status.is_terminal or existing.status is JobStatus.RUNNING
        ):
            logger.info(
                "execute_replay",
                extra={"plan_id": plan_id, "job_id": existing.job_id,
                       "job_status": existing.status.value},
            )
            return existing

        account, account_warnings = await self._account_snapshot()
        guard = BudgetGuard(
            budget_rub=plan.budget_rub,
            safety_margin=self.settings.budget_safety_margin,
            account=account,
        )

        recheck_warnings, blockers = await self._recheck_prices(plan, guard)
        warnings = [*account_warnings, *recheck_warnings]

        if blockers:
            job = _new_job(plan, warnings=warnings, errors=blockers)
            job.status = JobStatus.ABORTED
            for step in job.steps:
                step.status = StepStatus.SKIPPED
                step.error = "Исполнение отменено до списания."
            await self.jobs.save(job)
            await self.plans.attach_job(plan_id, job.job_id, plan.status.value)
            logger.warning(
                "execute_aborted",
                extra={"plan_id": plan_id, "job_id": job.job_id, "blockers": blockers},
            )
            return job

        job = _new_job(plan, warnings=warnings, errors=[])
        await self.jobs.save(job)
        plan.job_id = job.job_id
        plan.status = PlanStatus.EXECUTED
        await self.plans.save(plan)

        executor = Executor(
            client=self.client,
            job_repo=self.jobs,
            callback_url=self.settings.callback_url,
            poll_interval=self.settings.poll_interval_seconds,
            poll_timeout=self.settings.poll_timeout_seconds,
        )
        return await executor.run(plan, job, guard)

    async def _recheck_prices(
        self, plan: Plan, guard: BudgetGuard
    ) -> tuple[list[str], list[str]]:
        """Re-price every billable step; any increase blocks the whole execution."""
        warnings: list[str] = []
        blockers: list[str] = []
        total = 0.0

        for step in plan.steps:
            if step.kind is StepKind.LOCAL:
                continue
            try:
                payload = await self.client.estimate(step.estimate_body())
            except VibeError as exc:
                blockers.append(
                    f"Шаг «{step.step_id}»: повторная оценка недоступна ({exc}) — "
                    "исполнение остановлено без списания (fail-closed)."
                )
                continue

            if payload.get("valid") is False:
                blockers.append(
                    f"Шаг «{step.step_id}»: платформа считает запрос невалидным на момент запуска."
                )
                continue
            cost = payload.get("estimated_cost_rub")
            if not isinstance(cost, (int, float)):
                blockers.append(
                    f"Шаг «{step.step_id}»: платформа не вернула стоимость — списание запрещено."
                )
                continue

            cost = round(float(cost), 4)
            delta = round(cost - step.estimated_cost_rub, 4)
            if delta > PRICE_TOLERANCE_RUB:
                blockers.append(
                    f"Шаг «{step.step_id}» ({step.model}): цена изменилась с "
                    f"{step.estimated_cost_rub:.2f}₽ до {cost:.2f}₽ (+{delta:.2f}₽) — "
                    "исполнение отменено без списания."
                )
                continue
            if delta < -PRICE_TOLERANCE_RUB:
                warnings.append(
                    f"Шаг «{step.step_id}» ({step.model}): цена снизилась с "
                    f"{step.estimated_cost_rub:.2f}₽ до {cost:.2f}₽ — исполняем по новой цене."
                )
                step.estimated_cost_rub = cost

            daily = payload.get("daily_spend")
            if isinstance(daily, dict) and daily.get("within_limit") is False:
                blockers.append(
                    f"Шаг «{step.step_id}»: платформа сообщает о превышении дневного лимита."
                )
                continue
            _merge_account(guard.account, payload)
            total += cost

        total = round(total, 4)
        if total > guard.spendable_rub:
            blockers.append(
                f"Итоговая стоимость {total:.2f}₽ превышает безопасный лимит "
                f"{guard.spendable_rub:.2f}₽ (бюджет {guard.budget_rub:.2f}₽ минус резерв "
                f"{guard.margin_rub:.2f}₽) — списание не выполняется."
            )
        account = guard.account
        if account is None or account.balance_rub is None:
            blockers.append("Баланс аккаунта неизвестен — списание запрещено (fail-closed).")
        elif total > account.balance_rub:
            blockers.append(
                f"Итоговая стоимость {total:.2f}₽ превышает баланс {account.balance_rub:.2f}₽."
            )
        elif (
            account.daily_remaining_rub is not None and total > account.daily_remaining_rub
        ):
            blockers.append(
                f"Итоговая стоимость {total:.2f}₽ превышает остаток дневного лимита "
                f"{account.daily_remaining_rub:.2f}₽."
            )
        return warnings, blockers

    # ------------------------------------------------------------------
    async def get_job(self, job_id: str) -> Job:
        job = await self.jobs.get(job_id)
        if job is None:
            raise NotFoundError(f"Задание {job_id} не найдено.")
        return job

    async def get_plan(self, plan_id: str) -> Plan:
        plan = await self.plans.get(plan_id)
        if plan is None:
            raise NotFoundError(f"План {plan_id} не найден.")
        return plan


def _new_job(plan: Plan, *, warnings: list[str], errors: list[str]) -> Job:
    return Job(
        job_id=str(uuid.uuid4()),
        plan_id=plan.plan_id,
        mode=plan.mode,
        status=JobStatus.PENDING,
        budget_rub=plan.budget_rub,
        estimated_cost_rub=round(sum(s.estimated_cost_rub for s in plan.steps), 4),
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
        warnings=warnings,
        errors=errors,
    )


def _first_number(payload: dict[str, Any], keys: tuple[str, ...]) -> float | None:
    for key in keys:
        value = payload.get(key)
        if isinstance(value, bool):
            continue
        if isinstance(value, (int, float)):
            return float(value)
        if isinstance(value, dict):
            nested = _first_number(value, ("rub", "current", "amount", "value"))
            if nested is not None:
                return nested
    return None


def _merge_account(account: AccountSnapshot | None, estimate_payload: dict[str, Any]) -> None:
    """``/generate/estimate`` echoes balance and daily-spend state — use it."""
    if account is None:
        return
    balance = estimate_payload.get("balance")
    if isinstance(balance, dict) and isinstance(balance.get("current"), (int, float)):
        account.balance_rub = float(balance["current"])
    daily = estimate_payload.get("daily_spend")
    if isinstance(daily, dict):
        if isinstance(daily.get("limit"), (int, float)):
            account.daily_limit_rub = float(daily["limit"])
        if isinstance(daily.get("today"), (int, float)):
            account.daily_spent_rub = float(daily["today"])
