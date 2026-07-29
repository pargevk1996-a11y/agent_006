#!/usr/bin/env python
"""Mock demo: brief → plan → confirmed execution, entirely offline.

Runs the real planner, budget guard, executor and SQLite storage against the
mock client (a recorded snapshot of GET /capabilities). No token, no network,
no spending. Usage::

    uv run python scripts/demo_mock.py [--budget 400]
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.clients.mock import MockVibeClient
from app.core.config import AppMode, Settings
from app.core.logging import configure_logging
from app.domain.brief import Brief
from app.domain.policy import Policy
from app.repositories.db import Database
from app.repositories.jobs import JobRepository
from app.repositories.plans import PlanRepository
from app.services.plan_service import PlanService

BRIEF = {
    "product_name": "CRM для мастеров маникюра",
    "product_description": (
        "Онлайн-запись клиентов, автоматические напоминания в мессенджерах и учёт расходников "
        "в одном окне. Работает с телефона, настройка за 15 минут."
    ),
    "target_audience": "Мастера маникюра и небольшие студии красоты, 22–40 лет, Россия",
    "offer": "Первый месяц бесплатно и перенос базы клиентов силами поддержки",
    "formats": ["text", "image", "voice", "video"],
    "style": "дружелюбный, живой, без канцелярита",
    "aspect_ratio": "9:16",
}

RULER = "─" * 78


def hr(title: str) -> None:
    print(f"\n{RULER}\n {title}\n{RULER}")


async def main(budget: float) -> int:
    configure_logging("WARNING", "console")
    with tempfile.TemporaryDirectory() as tmp:
        database = Database(Path(tmp) / "demo.db")
        await database.connect()
        settings = Settings(app_mode=AppMode.MOCK, db_path=Path(tmp) / "demo.db")
        client = MockVibeClient(
            balance_rub=settings.mock_balance_rub,
            daily_limit_rub=settings.mock_daily_limit_rub,
        )
        service = PlanService(
            client=client,
            settings=settings,
            policy=Policy.load(None),
            plan_repo=PlanRepository(database),
            job_repo=JobRepository(database),
        )

        hr(f"1. ПЛАН (бюджет {budget:.0f} ₽, списаний нет)")
        plan = await service.create_plan(Brief(**BRIEF, budget_rub=budget))
        print(f"plan_id: {plan.plan_id}")
        print(f"статус : {plan.status.value}")
        print(
            f"бюджет : {plan.budget_rub:.2f} ₽ | к распределению {plan.spendable_rub:.2f} ₽ "
            f"(резерв на дрейф цены {plan.safety_margin_rub:.2f} ₽)"
        )
        print(
            f"итого  : {plan.total_estimated_rub:.2f} ₽ | "
            f"остаток {plan.budget_remaining_rub:.2f} ₽"
        )
        print(
            f"аккаунт: баланс {plan.account.balance_rub} ₽, дневной лимит "
            f"{plan.account.daily_limit_rub} ₽ (mock)"
        )
        for step in plan.steps:
            print(f"\n • {step.step_id} → {step.model or 'локальный шаг'}  "
                  f"[{step.estimated_cost_rub:.2f} ₽, источник цены: {step.cost_source}]")
            print(f"   почему: {step.reason}")
            if step.rejected_alternatives:
                print("   отклонено:")
                for alt in step.rejected_alternatives[:3]:
                    print(f"     – {alt.model}: {alt.reason}")
            for warning in step.warnings:
                print(f"   ⚠ {warning}")
        if plan.warnings:
            print("\nПредупреждения плана:")
            for warning in plan.warnings:
                print(f" ⚠ {warning}")

        hr("2. ПОПЫТКА ИСПОЛНЕНИЯ БЕЗ ПОДТВЕРЖДЕНИЯ")
        try:
            await service.execute_plan(plan.plan_id, confirmed=False)
        except Exception as exc:
            print(f"отказано: {type(exc).__name__}: {exc}")

        hr("3. ИСПОЛНЕНИЕ С confirmed=true (mock, деньги не настоящие)")
        job = await service.execute_plan(plan.plan_id, confirmed=True)
        print(f"job_id : {job.job_id}")
        print(f"статус : {job.status.value}  за {job.duration_seconds} c")
        print(
            f"деньги : план {job.estimated_cost_rub:.2f} ₽ → факт {job.actual_cost_rub:.2f} ₽ "
            f"(бюджет {job.budget_rub:.2f} ₽, остаток {job.budget_remaining_rub:.2f} ₽)"
        )
        for step in job.steps:
            link = step.display_url or ("<локальный текст>" if step.local_output else "—")
            print(
                f" • {step.step_id:<10} {step.status.value:<10} "
                f"{step.actual_cost_rub:>7.2f} ₽  {link}"
            )
            if step.error:
                print(f"   ошибка: {step.error}")

        hr("4. ПОВТОРНЫЙ EXECUTE (защита от двойного списания)")
        again = await service.execute_plan(plan.plan_id, confirmed=True)
        print(f"тот же job_id: {again.job_id == job.job_id}")
        print(f"факт списано повторно: {again.actual_cost_rub - job.actual_cost_rub:.2f} ₽")
        print(f"вызовов POST /generate у клиента всего: {len(client.calls_of('generate'))}")
        print(f"остаток mock-баланса: {client.balance_rub:.2f} ₽")

        assert job.actual_cost_rub <= plan.budget_rub, "budget invariant violated"
        await database.close()
        print("\n✅ Бюджет не превышен, повторное списание не выполнено.")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--budget", type=float, default=400.0, help="бюджет в рублях")
    args = parser.parse_args()
    raise SystemExit(asyncio.run(main(args.budget)))
