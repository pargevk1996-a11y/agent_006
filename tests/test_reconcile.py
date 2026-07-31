"""Досмотр оплаченных, но неотчитавшихся шагов.

Таймаут ожидания — это не «шаг провалился», а «мы не дождались»: деньги уже
списаны, генерация продолжается. Отчёт, который называет такой шаг failed, врёт
про потраченное. Здесь проверяется, что досмотрщик исправляет отчёт постфактум
и при этом ничего не запускает и не тратит.
"""

from __future__ import annotations

import asyncio

from app.clients.exceptions import VibeAPIError
from app.domain.plan import JobStatus, StepStatus
from app.repositories.jobs import JobRepository
from app.services.reconciler import Reconciler
from tests.conftest import make_brief, make_service, make_settings

#: Later than any ISO timestamp the ledger can hold — "no age filter at all".
ANY_AGE = "9999-01-01T00:00:00+00:00"


def make_reconciler(client, database, *, min_age: float = 0.0) -> Reconciler:
    return Reconciler(
        client=client,
        job_repo=JobRepository(database),
        interval_seconds=0,  # swept by hand in tests
        min_age_seconds=min_age,
    )


async def timed_out_job(client, database, tmp_path):
    """A job whose only step was launched, charged and never reported back."""
    settings = make_settings(tmp_path, poll_timeout_seconds=0.001, poll_interval_seconds=0.001)
    client.complete_after_polls = 10_000  # the platform keeps saying "processing"
    service = make_service(client, settings, database)
    plan = await service.create_plan(make_brief(formats=["image"], budget_rub=500.0))
    job = await service.execute_plan(plan.plan_id, confirmed=True, wait=True)
    client.complete_after_polls = 0  # ...and finishes right after we gave up
    return service, job


class TestStaleStepDiscovery:
    async def test_timed_out_step_is_left_running_in_the_ledger(
        self, client, database, tmp_path
    ):
        _, job = await timed_out_job(client, database, tmp_path)
        assert job.status is JobStatus.FAILED
        assert job.steps[0].status is StepStatus.FAILED
        assert "Таймаут" in job.steps[0].error

        stale = await JobRepository(database).stale_steps(updated_before=ANY_AGE)
        assert [s.step_id for s in stale] == ["image-1"]
        assert stale[0].generation_id is not None

    async def test_steps_of_a_live_job_are_not_touched(self, service, client, database):
        """While a job runs, an executor owns its steps — the sweeper keeps off."""
        plan = await service.create_plan(make_brief(formats=["image"], budget_rub=500.0))
        job = await service.execute_plan(plan.plan_id, confirmed=True, wait=True)
        await JobRepository(database).requeue(job.job_id)

        assert await JobRepository(database).stale_steps(updated_before=ANY_AGE) == []

    async def test_min_age_protects_fresh_steps(self, client, database, tmp_path):
        await timed_out_job(client, database, tmp_path)
        polls_before = len(client.calls_of("generation_status"))
        reconciler = make_reconciler(client, database, min_age=3600)

        assert await reconciler.sweep() == 0
        assert len(client.calls_of("generation_status")) == polls_before, (
            "свежий шаг не опрашивается: возможно, executor ещё ждёт его сам"
        )


class TestReconcileRewritesTheReport:
    async def test_completed_generation_replaces_the_timeout(self, client, database, tmp_path):
        service, job = await timed_out_job(client, database, tmp_path)
        charged = client.balance_rub
        generate_calls = len(client.calls_of("generate"))

        settled = await make_reconciler(client, database).sweep()

        assert settled == 1
        fixed = await service.get_job(job.job_id)
        assert fixed.status is JobStatus.SUCCEEDED
        assert fixed.steps[0].status is StepStatus.SUCCEEDED
        assert fixed.steps[0].display_url
        assert fixed.steps[0].error is None
        assert fixed.errors == [], "жалоба на таймаут больше не соответствует истине"
        assert any("досмотром" in w for w in fixed.warnings)
        # A sweep only reads status: nothing launched, nothing charged.
        assert len(client.calls_of("generate")) == generate_calls
        assert client.balance_rub == charged

    async def test_reconciled_step_leaves_the_stale_queue(self, client, database, tmp_path):
        await timed_out_job(client, database, tmp_path)
        reconciler = make_reconciler(client, database)

        assert await reconciler.sweep() == 1
        assert await reconciler.sweep() == 0, "повторный досмотр не должен ничего находить"

    async def test_actual_cost_survives_the_rewrite(self, client, database, tmp_path):
        service, job = await timed_out_job(client, database, tmp_path)
        cost_at_launch = job.steps[0].actual_cost_rub
        assert cost_at_launch > 0, "деньги списаны при запуске, до таймаута"

        await make_reconciler(client, database).sweep()

        fixed = await service.get_job(job.job_id)
        assert fixed.steps[0].actual_cost_rub == cost_at_launch
        assert fixed.actual_cost_rub == cost_at_launch
        assert fixed.actual_cost_rub <= fixed.budget_rub

    async def test_refunded_failure_is_recorded_as_zero(self, client, database, tmp_path):
        settings = make_settings(
            tmp_path, poll_timeout_seconds=0.001, poll_interval_seconds=0.001
        )
        client.complete_after_polls = 10_000
        service = make_service(client, settings, database)
        plan = await service.create_plan(make_brief(formats=["image"], budget_rub=500.0))
        job = await service.execute_plan(plan.plan_id, confirmed=True, wait=True)
        # The generation eventually fails upstream and the platform refunds it.
        client.complete_after_polls = 0
        client._generations[job.steps[0].generation_id]["will_fail"] = True

        await make_reconciler(client, database).sweep()

        fixed = await service.get_job(job.job_id)
        assert fixed.steps[0].status is StepStatus.FAILED
        assert fixed.steps[0].actual_cost_rub == 0.0
        assert fixed.actual_cost_rub == 0.0
        assert any("провайдер вернул ошибку" in e for e in fixed.errors)

    async def test_unknown_generation_is_settled_instead_of_chased_forever(
        self, client, database, tmp_path
    ):
        service, job = await timed_out_job(client, database, tmp_path)
        client._generations.pop(job.steps[0].generation_id)  # платформа её не знает

        reconciler = make_reconciler(client, database)
        assert await reconciler.sweep() == 1
        assert await reconciler.sweep() == 0

        fixed = await service.get_job(job.job_id)
        assert fixed.steps[0].status is StepStatus.FAILED
        assert "404" in fixed.steps[0].error

    async def test_still_processing_step_is_left_alone(self, client, database, tmp_path):
        service, job = await timed_out_job(client, database, tmp_path)
        client.complete_after_polls = 10_000  # платформа всё ещё работает

        assert await make_reconciler(client, database).sweep() == 0
        unchanged = await service.get_job(job.job_id)
        assert unchanged.steps[0].status is StepStatus.FAILED
        assert await JobRepository(database).stale_steps(updated_before=ANY_AGE) != []


class TestSweepIsRobust:
    async def test_one_unreachable_generation_does_not_stop_the_sweep(
        self, client, database, tmp_path
    ):
        service, job = await timed_out_job(client, database, tmp_path)
        original = client.generation_status

        async def flaky(generation_id):
            raise VibeAPIError(503, code="upstream_unavailable", message="провайдер лежит")

        client.generation_status = flaky
        assert await make_reconciler(client, database).sweep() == 0

        client.generation_status = original
        assert await make_reconciler(client, database).sweep() == 1
        assert (await service.get_job(job.job_id)).status is JobStatus.SUCCEEDED

    async def test_loop_survives_a_failing_sweep(self, client, database):
        reconciler = make_reconciler(client, database)
        reconciler.interval_seconds = 0.001
        calls: list[int] = []

        async def exploding_sweep() -> int:
            calls.append(1)
            raise RuntimeError("сверка упала")

        reconciler.sweep = exploding_sweep
        await reconciler.start()
        await asyncio.sleep(0.05)
        await reconciler.stop()

        assert len(calls) > 1, "цикл должен пережить падение отдельной сверки"
        assert reconciler.is_running is False

    async def test_zero_interval_disables_the_loop(self, client, database):
        reconciler = make_reconciler(client, database)
        await reconciler.start()
        assert reconciler.is_running is False
        await reconciler.stop()
