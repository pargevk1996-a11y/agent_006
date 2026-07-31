"""Prometheus exposition for the spend dashboard.

Numbers come from the database, not from in-process counters: for a service
trusted with money, "сколько потрачено" has to survive a restart and must never
disagree with the job reports. The cost is a few aggregate queries per scrape —
acceptable at this scale, and noted as a trade-off rather than hidden.

The endpoint exposes spending, not secrets, but it still describes someone's
budget: keep it on an internal route, not on the public ingress.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from fastapi.responses import PlainTextResponse

from app.api.deps import get_database, get_job_queue, get_settings
from app.api.routers.health import VERSION
from app.core.config import Settings
from app.core.tracing import tracing_status
from app.domain.plan import JobStatus, PlanStatus, StepStatus
from app.repositories.db import Database
from app.repositories.metrics import MetricsRepository, MetricsSnapshot
from app.services.job_queue import JobQueue

router = APIRouter(tags=["ops"])

PROMETHEUS_CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"


@router.get(
    "/metrics",
    response_class=PlainTextResponse,
    summary="Метрики расхода в формате Prometheus",
    description=(
        "Плановая и фактическая стоимость, состояния планов/заданий/шагов, возвраты и "
        "глубина очереди. Значения считаются из БД, поэтому переживают перезапуск и "
        "совпадают с отчётами по заданиям. Доли (aborted, refunded) не публикуются "
        "готовыми — они считаются запросом поверх счётчиков, как принято в Prometheus."
    ),
)
async def metrics(
    settings: Settings = Depends(get_settings),
    database: Database = Depends(get_database),
    queue: JobQueue | None = Depends(get_job_queue),
) -> PlainTextResponse:
    snapshot = await MetricsRepository(database).snapshot()
    body = _render(
        snapshot,
        mode=settings.app_mode.value,
        queue_depth=queue.depth if queue else 0,
        workers=queue.concurrency if queue else 0,
    )
    return PlainTextResponse(body, media_type=PROMETHEUS_CONTENT_TYPE)


def _render(snapshot: MetricsSnapshot, *, mode: str, queue_depth: int, workers: int) -> str:
    lines: list[str] = []

    _family(
        lines,
        "vibe_app_info",
        "gauge",
        "Режим и версия процесса.",
        [({"mode": mode, "version": VERSION, "tracing": tracing_status()}, 1)],
    )

    # -- money -------------------------------------------------------------
    _family(
        lines,
        "vibe_plan_estimated_rub_total",
        "counter",
        "Сумма плановой стоимости всех построенных планов, ₽.",
        [({}, snapshot.plan_estimated_rub)],
    )
    _family(
        lines,
        "vibe_job_estimated_rub_total",
        "counter",
        "Сумма плановой стоимости всех заданий, ₽.",
        [({}, snapshot.job_estimated_rub)],
    )
    _family(
        lines,
        "vibe_job_actual_rub_total",
        "counter",
        "Сумма фактически списанного по всем заданиям, ₽.",
        [({}, snapshot.job_actual_rub)],
    )
    _family(
        lines,
        "vibe_refunded_steps_total",
        "counter",
        "Шаги, деньги за которые вернула платформа.",
        [({}, snapshot.refunded_steps)],
    )

    # -- states ------------------------------------------------------------
    _family(
        lines,
        "vibe_plans_total",
        "counter",
        "Планы по статусам.",
        _by_status(snapshot.plans_by_status, PlanStatus),
    )
    _family(
        lines,
        "vibe_jobs_total",
        "counter",
        "Задания по статусам; aborted — остановленные бюджетным гардом до списания.",
        _by_status(snapshot.jobs_by_status, JobStatus),
    )
    _family(
        lines,
        "vibe_steps_total",
        "counter",
        "Шаги в ledger по статусам.",
        _by_status(snapshot.steps_by_status, StepStatus),
    )

    # -- runtime -----------------------------------------------------------
    _family(lines, "vibe_queue_depth", "gauge", "Задания, ждущие воркера.", [({}, queue_depth)])
    _family(
        lines, "vibe_executor_workers", "gauge", "Размер пула воркеров.", [({}, workers)]
    )
    _family(
        lines,
        "vibe_idempotency_keys_total",
        "gauge",
        "Сохранённые ключи повтора; чистятся по IDEMPOTENCY_TTL_HOURS.",
        [({}, snapshot.idempotency_keys)],
    )
    _family(
        lines,
        "vibe_media_uploads_total",
        "gauge",
        "Живые загруженные файлы; протухшие ссылки удаляются.",
        [({}, snapshot.media_uploads)],
    )
    _family(
        lines,
        "vibe_webhook_events_total",
        "counter",
        "Принятые вебхуки с подтверждённой подписью.",
        [({}, snapshot.webhook_events)],
    )
    return "\n".join(lines) + "\n"


def _by_status(counts: dict[str, int], enum) -> list[tuple[dict[str, str], float]]:
    """Every known status, including the zeroes — a dashboard should not lose a line."""
    return [({"status": member.value}, counts.get(member.value, 0)) for member in enum]


def _family(
    lines: list[str],
    name: str,
    kind: str,
    help_text: str,
    samples: list[tuple[dict[str, str], float]],
) -> None:
    lines.append(f"# HELP {name} {help_text}")
    lines.append(f"# TYPE {name} {kind}")
    for labels, value in samples:
        rendered = ",".join(f'{k}="{_escape(str(v))}"' for k, v in sorted(labels.items()))
        suffix = f"{{{rendered}}}" if rendered else ""
        lines.append(f"{name}{suffix} {_number(value)}")


def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")


def _number(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else f"{value:.4f}"
