"""Structured logging with correlation IDs and mandatory secret redaction."""

from __future__ import annotations

import json
import logging
import sys
import uuid
from contextvars import ContextVar
from typing import Any

from app.core.security import redact, register_secret

_correlation_id: ContextVar[str | None] = ContextVar("correlation_id", default=None)

_RESERVED = set(
    logging.LogRecord("", 0, "", 0, "", None, None).__dict__
) | {"message", "asctime", "taskName"}


def new_correlation_id() -> str:
    return uuid.uuid4().hex[:16]


def set_correlation_id(value: str | None) -> str:
    cid = value or new_correlation_id()
    _correlation_id.set(cid)
    return cid


def get_correlation_id() -> str | None:
    return _correlation_id.get()


class RedactionFilter(logging.Filter):
    """Last line of defence: scrub secrets from the message and every extra field."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = redact(record.msg)
        if record.args:
            if isinstance(record.args, dict):
                record.args = {k: _redact_value(v) for k, v in record.args.items()}
            else:
                record.args = tuple(_redact_value(a) for a in record.args)
        for key, value in list(record.__dict__.items()):
            if key not in _RESERVED:
                record.__dict__[key] = _redact_value(value)
        return True


def _redact_value(value: Any) -> Any:
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, dict):
        return {k: _redact_value(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return type(value)(_redact_value(v) for v in value)
    return value


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        cid = get_correlation_id()
        if cid:
            payload["correlation_id"] = cid
        for key, value in record.__dict__.items():
            if key not in _RESERVED and not key.startswith("_"):
                payload[key] = value
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return redact(json.dumps(payload, ensure_ascii=False, default=str))


class ConsoleFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        cid = get_correlation_id()
        prefix = f"[{cid}] " if cid else ""
        extras = {
            k: v
            for k, v in record.__dict__.items()
            if k not in _RESERVED and not k.startswith("_")
        }
        tail = " " + json.dumps(extras, ensure_ascii=False, default=str) if extras else ""
        stamp = self.formatTime(record, "%H:%M:%S")
        base = (
            f"{stamp} {record.levelname:<7} {prefix}"
            f"{record.name}: {record.getMessage()}{tail}"
        )
        if record.exc_info:
            base += "\n" + self.formatException(record.exc_info)
        return redact(base)


def configure_logging(
    level: str = "INFO", fmt: str = "json", secrets: list[str] | None = None
) -> None:
    for secret in secrets or []:
        register_secret(secret)

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter() if fmt == "json" else ConsoleFormatter())
    handler.addFilter(RedactionFilter())

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level.upper())

    # uvicorn keeps its own handlers; funnel them through ours so nothing bypasses redaction.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access", "httpx", "httpcore"):
        lg = logging.getLogger(name)
        lg.handlers.clear()
        lg.propagate = True
    logging.getLogger("httpx").setLevel("WARNING")
    logging.getLogger("httpcore").setLevel("WARNING")
