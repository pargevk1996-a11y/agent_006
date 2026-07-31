"""Метрики расхода и доменные спаны.

Для сервиса, которому доверили ключ с деньгами, «сколько потрачено» — не
служебная телеметрия, а часть отчётности: цифры должны переживать перезапуск и
совпадать с отчётами по заданиям. Поэтому метрики считаются из БД, и здесь это
проверяется в паре с настоящим экспортом заданий.
"""

from __future__ import annotations

import pytest

from app.core import tracing
from app.repositories.metrics import MetricsRepository
from tests.conftest import execute_and_wait, make_brief

BRIEF = {
    "product_name": "CRM для мастеров маникюра",
    "product_description": "Онлайн-запись, напоминания и учёт расходников.",
    "target_audience": "Мастера маникюра, 22–40 лет",
    "offer": "Первый месяц бесплатно",
    "formats": ["image"],
    "budget_rub": 300,
}


def parse_metrics(body: str) -> dict[str, float]:
    """Плоский разбор экспозиции Prometheus: 'имя{метки} значение'."""
    values: dict[str, float] = {}
    for line in body.splitlines():
        if line.startswith("#") or not line.strip():
            continue
        name, _, raw = line.rpartition(" ")
        values[name.strip()] = float(raw)
    return values


class TestExposition:
    async def test_endpoint_is_valid_prometheus_text(self, api_client):
        response = await api_client.get("/metrics")

        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/plain")
        body = response.text
        assert "# HELP vibe_job_actual_rub_total" in body
        assert "# TYPE vibe_job_actual_rub_total counter" in body
        assert body.endswith("\n")

    async def test_known_statuses_are_present_even_at_zero(self, api_client):
        """Пропавшая серия ломает дашборд — нули должны публиковаться."""
        values = parse_metrics((await api_client.get("/metrics")).text)

        for status in ("ready", "degraded", "infeasible", "executed"):
            assert values[f'vibe_plans_total{{status="{status}"}}'] == 0
        for status in ("queued", "running", "succeeded", "partial", "failed", "aborted"):
            assert values[f'vibe_jobs_total{{status="{status}"}}'] == 0

    async def test_app_info_reports_mode_and_tracing(self, api_client):
        body = (await api_client.get("/metrics")).text

        info = next(line for line in body.splitlines() if line.startswith("vibe_app_info"))
        assert 'mode="mock"' in info
        assert "tracing=" in info
        assert info.endswith(" 1")


class TestNumbersMatchTheReports:
    async def test_spend_matches_the_job_report(self, api_client):
        plan = (await api_client.post("/api/v1/plans", json=BRIEF)).json()
        job = await execute_and_wait(api_client, plan["plan_id"])

        values = parse_metrics((await api_client.get("/metrics")).text)

        assert values["vibe_job_actual_rub_total"] == pytest.approx(job["actual_cost_rub"])
        assert values["vibe_job_estimated_rub_total"] == pytest.approx(
            job["estimated_cost_rub"]
        )
        assert values["vibe_plan_estimated_rub_total"] == pytest.approx(
            plan["total_estimated_rub"]
        )
        assert values['vibe_jobs_total{status="succeeded"}'] == 1
        assert values['vibe_steps_total{status="succeeded"}'] == 1

    async def test_aborted_run_is_counted_and_costs_nothing(self, client, service, database):
        plan = await service.create_plan(make_brief(formats=["image"], budget_rub=500.0))
        client.price_multiplier = 2.0
        job = await service.execute_plan(plan.plan_id, confirmed=True)

        snapshot = await MetricsRepository(database).snapshot()

        assert job.status.value == "aborted"
        assert snapshot.jobs_by_status["aborted"] == 1
        assert snapshot.job_actual_rub == 0.0
        assert snapshot.plan_estimated_rub > 0, "план построен, но ни рубля не потрачено"

    async def test_refunded_steps_are_visible(self, client, service, database):
        """Возврат не виден в сумме списаний — иначе сбой провайдера не отличить от нашей ошибки."""
        plan = await service.create_plan(
            make_brief(formats=["image"], budget_rub=500.0)
        )
        client.fail_models = {plan.steps[0].model}
        job = await service.execute_plan(plan.plan_id, confirmed=True)

        snapshot = await MetricsRepository(database).snapshot()

        assert job.actual_cost_rub == 0.0
        assert snapshot.refunded_steps == 1

    async def test_counts_survive_a_restart(self, api_client, tmp_path):
        """Счётчики берутся из БД, а не из памяти процесса."""
        import httpx

        from app.main import create_app
        from tests.conftest import make_settings

        plan = (await api_client.post("/api/v1/plans", json=BRIEF)).json()
        await execute_and_wait(api_client, plan["plan_id"])
        before = parse_metrics((await api_client.get("/metrics")).text)

        # Новый процесс поверх того же файла БД — как после перезапуска.
        settings = make_settings(tmp_path, db_path=tmp_path / "api.db")
        app = create_app(settings)
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as fresh:
                after = parse_metrics((await fresh.get("/metrics")).text)

        assert after["vibe_job_actual_rub_total"] == pytest.approx(
            before["vibe_job_actual_rub_total"]
        )
        assert after["vibe_job_actual_rub_total"] > 0
        assert after['vibe_jobs_total{status="succeeded"}'] == 1


class TestTracing:
    def test_disabled_by_default_and_never_raises(self):
        with tracing.span("plan.create", budget_rub=100.0) as trace:
            if trace is not None:
                trace.set(plan_status="ready")
        assert tracing.is_recording() is False

    def test_none_attributes_are_dropped(self, otel_spans):
        with tracing.span("step.generate", model=None, step_id="image-1"):
            pass

        attributes = otel_spans()[0].attributes
        assert "model" not in attributes, "пустой тег — это шум"
        assert attributes["step_id"] == "image-1"

    def test_span_carries_the_correlation_id(self, otel_spans):
        from app.core.logging import set_correlation_id

        set_correlation_id("cid-observability")
        with tracing.span("job.run", job_id="j-1"):
            pass

        assert otel_spans()[0].attributes["correlation_id"] == "cid-observability"

    async def test_money_path_emits_domain_spans(self, otel_spans, service, client):
        plan = await service.create_plan(make_brief(formats=["image"], budget_rub=500.0))
        await service.execute_plan(plan.plan_id, confirmed=True, wait=True)

        by_name = {s.name: s for s in otel_spans()}
        assert {"plan.create", "job.run", "step.generate"} <= set(by_name)

        step = by_name["step.generate"]
        assert step.attributes["model"] == plan.steps[0].model
        assert step.attributes["estimated_cost_rub"] == plan.steps[0].estimated_cost_rub
        assert step.attributes["actual_cost_rub"] > 0
        assert step.attributes["step_status"] == "succeeded"

        assert by_name["plan.create"].attributes["plan_id"] == plan.plan_id
        assert by_name["job.run"].attributes["job_status"] == "succeeded"

    async def test_a_failed_step_still_closes_its_span(self, otel_spans, service, client):
        plan = await service.create_plan(make_brief(formats=["image"], budget_rub=500.0))
        client.fail_models = {plan.steps[0].model}
        await service.execute_plan(plan.plan_id, confirmed=True, wait=True)

        step = next(s for s in otel_spans() if s.name == "step.generate")
        assert step.attributes["step_status"] == "failed"
        assert step.attributes["refunded"] is True
