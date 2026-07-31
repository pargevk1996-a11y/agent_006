"""``Idempotency-Key`` support for this service's own endpoints.

Everything downstream is already protected: a step is claimed in the ledger
before it is launched, a job is claimed in its row before it runs. What none of
that covers is the caller: a client whose connection dropped after ``/execute``
does not know whether the plan was admitted, and a blind retry is exactly the
situation the whole project exists to prevent.

With a key, a retry is answered with the *stored response of the first attempt*
instead of doing the work again. Without one, behaviour is unchanged.
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from app.core.errors import ConflictError
from app.repositories.idempotency import IdempotencyRecord, IdempotencyRepository

logger = logging.getLogger(__name__)

#: Tells the caller whether this answer was produced now or replayed from storage.
REPLAY_HEADER = "Idempotency-Replayed"

Produce = Callable[[], Awaitable[tuple[BaseModel, int]]]


def body_digest(body: Any) -> str:
    canonical = json.dumps(body, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:32]


async def with_idempotency(
    *,
    store: IdempotencyRepository,
    key: str | None,
    endpoint: str,
    body: Any,
    response: Response,
    produce: Produce,
) -> BaseModel | JSONResponse:
    """Run ``produce`` at most once per key, replaying its answer afterwards."""
    if not key:
        model, status_code = await produce()
        response.status_code = status_code
        return model

    digest = body_digest(body)
    existing = await store.claim(key=key, endpoint=endpoint, request_hash=digest)
    if existing is not None:
        return _replay(existing, endpoint=endpoint, digest=digest)

    try:
        model, status_code = await produce()
    except BaseException:
        # A refused or failed attempt must not poison the key: the caller has to
        # be able to fix the request and retry with the same one.
        await store.release(key)
        raise

    payload = model.model_dump(mode="json")
    headers = dict(response.headers)
    await store.complete(key, status_code=status_code, headers=headers, body=payload)
    response.status_code = status_code
    response.headers[REPLAY_HEADER] = "false"
    return model


def _replay(record: IdempotencyRecord, *, endpoint: str, digest: str) -> JSONResponse:
    if record.endpoint != endpoint or record.request_hash != digest:
        raise ConflictError(
            "Этот Idempotency-Key уже использован для другого запроса. Повтор должен нести "
            "идентичное тело и адрес — иначе это новый запрос, и ему нужен новый ключ.",
            code="idempotency_key_reused",
        )
    if not record.is_complete:
        raise ConflictError(
            "Запрос с этим Idempotency-Key ещё выполняется — дождитесь ответа на первую "
            "попытку и повторите её при необходимости.",
            code="idempotency_key_in_flight",
        )
    logger.info("idempotent_replay", extra={"endpoint": endpoint, "status": record.status_code})
    return JSONResponse(
        status_code=record.status_code or 200,
        content=record.response_body,
        headers={**record.headers, REPLAY_HEADER: "true"},
    )
