"""Optional OpenTelemetry spans over the existing ``correlation_id``.

Deliberately thin. Transport, sampling and HTTP/ASGI instrumentation are left to
the standard launcher::

    uv run --extra otel opentelemetry-instrument \\
      --traces_exporter otlp --service_name vibe-budget-agent \\
      uvicorn app.main:app

What that cannot produce is the part that matters here: a span for *this* domain
carrying the money. ``step.generate`` with ``model`` and ``estimated_cost_rub`` on
it is what turns a trace into an answer to "почему этот запуск стоил 1188 ₽".

Everything degrades quietly: without the OTel packages, or with them installed
but no exporter configured, :func:`span` is a no-op and the service behaves
exactly as before. Tracing must never be a reason for a request to fail.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from app.core.logging import get_correlation_id

logger = logging.getLogger(__name__)

try:  # pragma: no cover - import shape depends on the environment
    from opentelemetry import trace as _otel_trace
except ImportError:  # pragma: no cover - the default, dependency-free install
    _otel_trace = None

TRACER_NAME = "vibe-budget-agent"


def tracing_status() -> str:
    """Three honest states, short enough to be a metric label.

    ``disabled`` — пакеты не установлены; ``available`` — установлены, но экспортёр
    не настроен, спаны никуда не уходят; ``otel`` — трейсинг работает.
    """
    if _otel_trace is None:
        return "disabled"
    provider = _otel_trace.get_tracer_provider()
    if type(provider).__name__ == "ProxyTracerProvider":
        return "available"
    return "otel"


def is_recording() -> bool:
    return _otel_trace is not None and tracing_status() == "otel"


@contextmanager
def span(name: str, **attributes: Any) -> Iterator[Any]:
    """Domain span with ``correlation_id`` and any money attached to it.

    ``None`` attribute values are dropped: an empty tag is noise, and a missing
    price is meaningfully different from a zero one.
    """
    if _otel_trace is None:
        yield None
        return

    tracer = _otel_trace.get_tracer(TRACER_NAME)
    with tracer.start_as_current_span(name) as current:
        _annotate(current, attributes)
        yield _SpanHandle(current)


class _SpanHandle:
    """Lets a caller record what it only learns at the end (actual cost, status)."""

    __slots__ = ("_span",)

    def __init__(self, span: Any) -> None:
        self._span = span

    def set(self, **attributes: Any) -> None:
        _annotate(self._span, attributes)


def _annotate(current: Any, attributes: dict[str, Any]) -> None:
    if current is None or not getattr(current, "is_recording", lambda: False)():
        return
    correlation_id = get_correlation_id()
    if correlation_id:
        attributes.setdefault("correlation_id", correlation_id)
    for key, value in attributes.items():
        if value is not None:
            current.set_attribute(key, value)
